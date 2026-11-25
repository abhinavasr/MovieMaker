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
import re
import shutil
import subprocess
import tempfile
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
MAKES = ("animate", "perform", "lipsync", "motion", "talk2", "longtalk", "recast", "restyle")
TALK_MAKES = ("talk2", "longtalk")
REMAKE_MAKES = ("recast", "restyle")
REMAKE_PIN = 12          # the source frame the painted picture is drawn from
DEFAULT_MIX = {"dialogue": {"gain_db": 0.0, "mute": False},
               "ambience": {"gain_db": -6.0, "mute": False},
               "music": {"gain_db": -8.0, "mute": False},
               # Sound a video model invents is not used: voices are generated separately
               # and the video is made from them (the user's rule, 2026-11-24).
               "shot": {"gain_db": -4.0, "mute": True},
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
_export_tasks: set = set()


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
    # The assistant applies its own edits in a new production; the person can switch to
    # reviewing proposals (⋯ menu). 2026-11-25: every storyboard batch waited for "apply"
    # (34 and 19 minutes in the first real session) and plans built on unapplied batches
    # went wrong; the person asked to minimise their involvement.
    params["agent_applies"] = True
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


def add_scene(project, title: str = "", summary: str = "", position: int | None = None,
              refs: list | None = None) -> str:
    pid = project["id"]
    scenes = scenes_of(pid)
    pos = len(scenes) if position is None else max(0, min(len(scenes), int(position)))
    for s in scenes[pos:]:
        db.run("UPDATE prod_scenes SET idx=? WHERE id=?", (s["idx"] + 1, s["id"]))
    sid = _new_scene(pid, title or f"Scene {len(scenes) + 1}", summary, pos)
    if refs:
        db.run("UPDATE prod_scenes SET refs=? WHERE id=?", (json.dumps(_valid_refs(pid, refs)), sid))
    _reindex_scenes(pid)
    _touch(pid)
    return sid


def _reindex_scenes(pid: str) -> None:
    for i, s in enumerate(scenes_of(pid)):
        if s["idx"] != i:
            db.run("UPDATE prod_scenes SET idx=? WHERE id=?", (i, s["id"]))


def edit_scene(project, scene_id: str, title: str | None = None, summary: str | None = None,
               position: int | None = None, refs: list | None = None,
               locked: bool | None = None) -> None:
    pid = project["id"]
    row = _scene(pid, scene_id)
    if locked is not None:
        db.run("UPDATE prod_scenes SET locked=? WHERE id=?", (1 if locked else 0, scene_id))
    if refs is not None:
        db.run("UPDATE prod_scenes SET refs=? WHERE id=?",
               (json.dumps(_valid_refs(pid, list(refs))), scene_id))
    db.run("UPDATE prod_scenes SET title=?, summary=? WHERE id=?",
           ((title if title is not None else row["title"])[:120],
            (summary if summary is not None else row["summary"])[:2000], scene_id))
    if position is not None:
        _not_rendering(project)
        scenes = [s for s in scenes_of(pid) if s["id"] != scene_id]
        pos = max(0, min(len(scenes), int(position)))
        order = [s["id"] for s in scenes]
        order.insert(pos, scene_id)
        before = timings(pid)
        for i, s in enumerate(order):
            db.run("UPDATE prod_scenes SET idx=? WHERE id=?", (i, s))
        _renumber(pid)
        _follow_shots(pid, before)
    _touch(pid)


def delete_scene(project, scene_id: str) -> None:
    pid = project["id"]
    _not_rendering(project)
    _scene(pid, scene_id)
    before = timings(pid)
    for s in db.query("SELECT id FROM shots WHERE scene_id=?", (scene_id,)):
        _delete_shot_row(pid, s["id"])
    db.run("DELETE FROM prod_scenes WHERE id=?", (scene_id,))
    _reindex_scenes(pid)
    if not scenes_of(pid):
        _new_scene(pid, "Scene 1", "", 0)
    _renumber(pid)
    _follow_shots(pid, before)
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
    if not refs:
        refs = _jl(_scene(pid, scene_id)["refs"], [])       # the scene group's cast and set
    shot_id = db.new_id("sht")
    before = timings(pid)
    params = db.params_of(project)
    make = make if make in MAKES else "animate"
    secs = _clamp(make, seconds if seconds is not None else
                  params.get("seconds", cfg.DEFAULT_SHOT_SECONDS))
    db.run("INSERT INTO shots (id, project_id, idx, title, prompt, seconds, scene_id, framing, "
           "refs, make, start_from) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
           (shot_id, pid, base, (title or "")[:80], prompt or "", secs, scene_id,
            framing if framing in FRAMINGS else "", json.dumps(_valid_refs(pid, refs or [])),
            make if make in MAKES else "animate", start_from if start_from in START_FROM else ""))
    if start_from in START_FROM and start_from:
        db.run("UPDATE shots SET cont_checked=1 WHERE id=?", (shot_id,))
    _renumber(pid)
    _follow_shots(pid, before)
    if image_id and user is not None:
        set_picture(project, user, shot_id, image_id)
    _touch(pid)
    return shot_id


def cap_seconds(make: str) -> int:
    return {"perform": cfg.SINGLE_MAX_SECONDS, "lipsync": cfg.LIPSYNC_MAX_SECONDS,
            "motion": cfg.MOTION_MAX_SECONDS, "talk2": cfg.TALK_MAX_SECONDS,
            "longtalk": cfg.TALK_MAX_SECONDS, "recast": cfg.REMAKE_MAX_SECONDS,
            "restyle": cfg.REMAKE_MAX_SECONDS}.get(
        make, cfg.STORYBOARD_MAX_SECONDS)


def _clamp(make: str, seconds) -> int:
    try:
        v = int(round(float(seconds)))
    except (TypeError, ValueError):
        v = cfg.DEFAULT_SHOT_SECONDS
    return max(4 if make in REMAKE_MAKES else 1, min(cap_seconds(make), v))


def _follow_shots(pid: str, before: dict[str, tuple[float, float]]) -> None:
    """Dialogue belongs to the shot it is spoken over: when shots move, grow, shrink, or
    others are inserted or removed, each line moves with its shot. Music and ambience
    are laid across scenes and stay where they were put."""
    after = timings(pid)
    for c in db.query("SELECT id, start_s FROM prod_clips WHERE project_id=? AND track='dialogue'",
                      (pid,)):
        owner = next((sid for sid, (st, ln) in before.items() if st <= c["start_s"] < st + ln), None)
        if owner is None or owner not in after:
            continue
        delta = after[owner][0] - before[owner][0]
        if abs(delta) > 1e-6:
            db.run("UPDATE prod_clips SET start_s=?, updated_at=? WHERE id=?",
                   (round(max(0.0, c["start_s"] + delta), 3), db.now(), c["id"]))


def _valid_refs(pid: str, refs: list) -> list:
    have = {r["id"] for r in db.query("SELECT id FROM prod_refs WHERE project_id=?", (pid,))}
    return [r for r in refs if r in have][:MAX_IMAGE_REFS]


def edit_shot(project, shot_id: str, **fields) -> None:
    pid = project["id"]
    shot = _shot(pid, shot_id)
    before = timings(pid)
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
        sets["cont_checked"] = 1              # decided (by the person or the planner): final
    if isinstance(fields.get("opts"), dict):
        sets["opts"] = json.dumps(_clean_opts(pid, {**_jl(shot["opts"], {}), **fields["opts"]}))
    changes_clip = any(k in sets for k in ("prompt", "seconds", "make", "refs", "start_from",
                                            "opts"))
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
        if "idx" in sets or "seconds" in sets or "scene_id" in sets:
            _follow_shots(pid, before)
        _touch(pid)


def _delete_shot_row(pid: str, shot_id: str) -> None:
    shot = db.one("SELECT * FROM shots WHERE id=?", (shot_id,))
    if shot and shot["clip_path"]:
        Path(shot["clip_path"]).unlink(missing_ok=True)
    db.run("DELETE FROM shots WHERE id=?", (shot_id,))


def delete_shot(project, shot_id: str) -> None:
    _not_rendering(project)
    _shot(project["id"], shot_id)
    before = timings(project["id"])
    _delete_shot_row(project["id"], shot_id)
    _renumber(project["id"])
    _follow_shots(project["id"], before)
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
            user: auth.User | None = None, voice_design: str = "") -> str:
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
    if image_id and kind in ("character", "location"):
        _want_angles(pid, rid)
    if kind in VOICE_KINDS and not voice_path and not speaker:
        # Part of creating a character: its own voice is designed from its description.
        db.run("UPDATE prod_refs SET voice_auto=1, voice_design=? WHERE id=?",
               (" ".join((voice_design or "").split())[:700], rid))
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
        if f["image_id"] != row["image_id"] and (f.get("kind") or row["kind"]) in ("character", "location"):
            sets["angles"] = json.dumps({"_auto": True})
    if f.get("voice_design") is not None:
        sets["voice_design"] = " ".join(str(f["voice_design"]).split())[:700]
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
        if "voice_path" in sets:
            # The person's own sample: its transcript is not known (Whisper hears it).
            db.run("UPDATE prod_refs SET voice_text=NULL WHERE id=?", (ref_id,))
        if "voice_path" in sets or ("speaker" in sets and
                                    (sets["speaker"] or None) != (row["speaker"] or None)):
            revoice_lines(ref_id)
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


def name_key(name: str) -> str:
    """One spelling for a name: "Daśaratha", "dasaratha" and "The  Dasaratha" match
    (2026-11-25: two storyboard batches each added Vālmīki, Nārada and Daśaratha, and
    the person had to delete the copies while their pictures were already drawing)."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    s = re.sub(r"[^\w]+", " ", s).strip()
    return re.sub(r"^(the|a|an) ", "", s)


def ref_by_name(pid: str, name: str):
    k = name_key(name)
    if not k:
        return None
    for r in db.query("SELECT * FROM prod_refs WHERE project_id=? ORDER BY idx", (pid,)):
        if name_key(r["name"]) == k:
            return r
    return None


def resolve_ref(pid: str, v) -> str | None:
    """A reference given by id or by name (the planner often only knows the name of a
    reference it has just added)."""
    if not isinstance(v, str) or not v.strip():
        return None
    if db.one("SELECT 1 FROM prod_refs WHERE id=? AND project_id=?", (v, pid)):
        return v
    r = ref_by_name(pid, v)
    return r["id"] if r else None


def library(user, exclude_pid: str | None = None, query: str = "",
            limit: int = 60) -> list[dict]:
    """The person's characters, places and voices from their other productions, newest
    first, one per name and picture (2026-11-25, the person: "allow users see all their
    previous characters ... and add them here for consistency across stories")."""
    q = name_key(query)
    rows = db.query(
        "SELECT r.*, p.title AS production, p.updated_at AS p_updated FROM prod_refs r "
        "JOIN projects p ON p.id=r.project_id WHERE p.user_id=? AND p.id!=? "
        "ORDER BY p.updated_at DESC, r.idx",
        (user if isinstance(user, str) else user.id, exclude_pid or ""))
    out, seen = [], set()
    for r in rows:
        k = (name_key(r["name"]), r["image_id"] or "", r["kind"])
        if k in seen:
            continue
        if q and q not in name_key(r["name"]) and q not in name_key(r["description"] or ""):
            continue
        seen.add(k)
        out.append({"ref_id": r["id"], "kind": r["kind"], "name": r["name"],
                    "description": r["description"] or "", "image_id": r["image_id"],
                    "has_picture": bool(r["image_id"] and _img_done(r["image_id"])),
                    "voice": bool(r["voice_path"]) or bool(r["speaker"]),
                    "production": r["production"]})
        if len(out) >= limit:
            break
    return out


def library_matches(project, refs=None, limit: int = 12) -> list[dict]:
    """Library entries whose name appears in this production's story (or its references):
    the only ones worth the planner's context (2026-11-25: listing every character by name
    alone, the planner imported a whole broken earlier attempt of the same story)."""
    text = " " + name_key((project["story"] or "") + " " +
                          " ".join(r["name"] for r in (refs or []))) + " "
    out = []
    for x in library(project["user_id"], project["id"], limit=200):
        k = name_key(x["name"])
        words = [w for w in k.split() if len(w) >= 4]
        if f" {k} " in text or (words and f" {words[-1]} " in text):
            out.append(x)
            if len(out) >= limit:
                break
    return out


def import_ref(project, user: auth.User, src_ref_id: str) -> str:
    """Copy one of the person's references from another production into this one:
    look, pictures and views, voice. The same name already here is filled in, not doubled."""
    pid = project["id"]
    src = db.one("SELECT r.* FROM prod_refs r JOIN projects p ON p.id=r.project_id "
                 "WHERE r.id=? AND p.user_id=?", (src_ref_id, user.id))
    if src is None:
        raise HTTPException(404, "no such character in your productions")
    have = ref_by_name(pid, src["name"])
    rid = have["id"] if have else db.new_id("ref")
    voice_path = None
    if src["voice_path"] and Path(src["voice_path"]).exists():
        dest = cfg.uploads_dir / pid / "voices"
        dest.mkdir(parents=True, exist_ok=True)
        voice_path = str(dest / f"{rid}{Path(src['voice_path']).suffix}")
        shutil.copyfile(src["voice_path"], voice_path)
    if have:
        db.run("UPDATE prod_refs SET kind=?, description=?, image_id=?, angles=?, speaker=?, "
               "voice_path=COALESCE(?, voice_path) WHERE id=?",
               (src["kind"], src["description"], src["image_id"], src["angles"] or "{}",
                src["speaker"], voice_path, rid))
    else:
        idx = (db.one("SELECT MAX(idx) AS m FROM prod_refs WHERE project_id=?", (pid,))["m"] or 0) + 1
        db.run("INSERT INTO prod_refs (id, project_id, idx, kind, name, description, image_id, "
               "voice_path, speaker, created_at, angles) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (rid, pid, idx, src["kind"], src["name"], src["description"] or "", src["image_id"],
                voice_path, src["speaker"], db.now(), src["angles"] or "{}"))
    _touch(pid)
    return rid


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
    sid = await _speak(user, ref, text, instruct, language, speaker)
    name = f"{ref['name']}: {text}" if ref else text
    return _insert_clip(pid, "dialogue", name=name, source_kind="speech", source_id=sid,
                        status="pending", start_s=start_s, use_in_video=use_in_video,
                        ref_id=ref_id, text=text)


async def _speak(user: auth.User, ref, text: str, instruct: str = "",
                 language: str | None = None, speaker: str | None = None) -> str:
    """Voice one line in a character's voice: their sample cloned, their preset, or a
    Hindi-only preset cloned from its own clip for other languages. Returns the speech id."""
    reference = Path(ref["voice_path"]).read_bytes() if ref and ref["voice_path"] and \
        Path(ref["voice_path"]).exists() else None
    voice = speaker or (ref["speaker"] if ref else None)
    # A designed voice knows what its sample says: cloning uses it (no Whisper needed).
    reference_text = (ref["voice_text"] if ref is not None and "voice_text" in ref.keys()
                      and reference else None) or None
    if not reference and voice in speech.HINDI_SPEAKERS and not speech._has_hindi(text) and \
            (language or "").lower() not in ("hindi", "hi"):
        # A Hindi-only preset speaking another language: clone that preset's own clip, so the
        # character keeps the same voice (2026-11-24: Pip = Anaya speaking English).
        clip, said = _preset_clip(voice)
        if clip:
            reference, reference_text = clip, said
            # Clone it on VoxCPM2 (which made these voices), not Qwen: Qwen's cross-language
            # clone of these clips drawled (9-14 s for an 8-word line); VoxCPM2 said it in
            # ~2 s, word-perfect. A "Hindi" clone routes to VoxCPM2, which speaks whatever
            # text it is given (the tag only picks the engine).
            language = "Hindi"
    ids = await speech.create(user, text, speaker=None if reference else voice,
                              language=language, instruct=None if reference else (instruct or None),
                              reference=reference, reference_text=reference_text)
    return ids[0]


def add_script(project, shot_id: str, text: str, ref_id: str | None = None, instruct: str = "",
               offset_s: float | None = None, auto: bool = True) -> str:
    """A line of the script under a shot, not voiced yet: it shows on the dialogue track
    and is voiced in the character's voice (automatically when `auto`)."""
    pid = project["id"]
    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "the line is empty")
    shot = _shot(pid, shot_id)
    start, length = timings(pid).get(shot["id"], (0.0, 0.0))
    if offset_s is None:
        ends = [c["start_s"] + (c["duration_s"] or 0) for c in
                db.query("SELECT start_s, duration_s FROM prod_clips WHERE project_id=? AND "
                         "track='dialogue'", (pid,))
                if start <= c["start_s"] < start + length]
        at = max(ends) + 0.3 if ends else start + 0.3
    else:
        at = start + max(0.0, float(offset_s))
    estimate = round(max(1.0, len(text.split()) / 2.6 + 0.4), 1)
    ref = db.one("SELECT name FROM prod_refs WHERE id=?", (ref_id,)) if ref_id else None
    cid = _insert_clip(pid, "dialogue", name=f"{ref['name']}: {text}" if ref else text,
                       source_kind="speech", status="script", start_s=at, use_in_video=True,
                       ref_id=ref_id, text=text, duration_s=estimate)
    db.run("UPDATE prod_clips SET instruct=?, auto=? WHERE id=?",
           (instruct[:300], 1 if auto else 0, cid))
    return cid


async def voice_script(project, user: auth.User, clip_id: str) -> None:
    """Voice a script line now (its length becomes the real spoken length)."""
    c = _clip(project["id"], clip_id)
    if c["status"] != "script":
        return
    ref = db.one("SELECT * FROM prod_refs WHERE id=?", (c["ref_id"],)) if c["ref_id"] else None
    if ref is not None and needs_voice(ref):
        # No voice of their own yet: never the default (2026-11-25: every character, women
        # included, spoke in the male default "Ryan"). Design theirs; the line waits for it.
        if not ref["voice_auto"]:
            db.run("UPDATE prod_refs SET voice_auto=1 WHERE id=?", (ref["id"],))
        return
    sid = await _speak(user, ref, c["text"], c["instruct"] or "")
    db.run("UPDATE prod_clips SET source_id=?, status='pending', duration_s=NULL, auto=0, error=NULL, "
           "updated_at=? WHERE id=?", (sid, db.now(), clip_id))
    _touch(project["id"])


def _preset_clip(voice: str) -> tuple[bytes | None, str | None]:
    base = Path("/srv/presets")
    wav = base / f"{voice}.wav"
    if not wav.exists():
        return None, None
    try:
        meta = json.loads((base / "presets.json").read_text())
    except (OSError, ValueError):
        meta = {}
    said = (meta.get("transcripts") or {}).get(voice) or meta.get("transcript")
    return wav.read_bytes(), said


# --------------------------------------------------------------------------- designed voices
# Every character gets a voice of their own, made from their description with Qwen3-TTS
# VoiceDesign, as part of creating the character (the person, 2026-11-25: "generating new audio
# profiles per character based on the character description ... bake it as part of agentic
# workflow when creating characters"). One short sample is designed; every line is then CLONED
# from it, so the character keeps the same voice.

VOICE_KINDS = ("character", "voice")

VOICE_BRIEF_RULES = """You write a VOICE DESCRIPTION for a text-to-speech voice designer, from a
character description. START with the gender and age in plain words ("A woman in her fifties",
"A man in his seventies"). Pitch must fit the gender: a woman's voice is medium-high or high
unless the description clearly says otherwise; a man's is low or medium.
One or two sentences, at most 50 words, covering: gender, age, pitch
(low/medium/high), timbre (e.g. warm, husky, bright, breathy, resonant), pace, accent (only when
the story's setting implies one, e.g. "a soft Indian English accent"), and the emotional colour
of how they usually speak. Describe the VOICE only: no looks, no clothes, no names.
A baby or an animal that does not talk: describe the sound they would make.
Answer with JSON: {"voice": "..."}"""


def revoice_lines(ref_id: str) -> int:
    """The character's voice changed: their spoken lines are voiced again in the new voice
    (2026-11-25: presets were given to the characters after their lines had been spoken in the
    default voice, and nothing followed)."""
    n = 0
    for c in db.query("SELECT id FROM prod_clips WHERE ref_id=? AND source_kind='speech' AND "
                      "text IS NOT NULL AND status IN ('ready','pending','failed')", (ref_id,)):
        db.run("UPDATE prod_clips SET status='script', auto=1, error=NULL WHERE id=?", (c["id"],))
        n += 1
    return n


def needs_voice(r) -> bool:
    return (r["kind"] in VOICE_KINDS and not r["voice_path"] and not r["speaker"]
            and (r["voice_design"] or "") != "off")


def _guess_brief(r) -> str:
    """When no model is free: at least the right gender and age."""
    d = f" {(r['name'] or '')} {(r['description'] or '')} ".lower()
    female = re.search(r"\b(she|her|woman|women|girl|queen|princess|mother|wife|daughter|"
                       r"lady|female|goddess|sister|grandmother|aunt)\b", d)
    old = re.search(r"\b(old|elder|elderly|aged|grey|gray|silver|grand)", d)
    child = re.search(r"\b(child|boy|girl|kid|teen|teenage|young)\b", d)
    baby = re.search(r"\b(newborn|baby|infant)\b", d)
    if baby:
        return "A newborn baby: soft coos and small cries, very high and gentle."
    who = "woman" if female else "man"
    age = "elderly " if old else ("young " if child else "adult ")
    pitch = ("a warm, medium-high" if female else "a warm, low") + " voice"
    return f"An {age}{who} with {pitch}, clear and natural, speaking at a calm, steady pace."


async def voice_brief(project, r) -> str:
    """The voice description: the planner's (stored on the reference) or written now by the
    small CPU model from the character description; a rule-based guess as the last resort."""
    if (r["voice_design"] or "").strip() and r["voice_design"] != "off":
        return r["voice_design"].strip()
    from app import agent
    try:
        res = await agent._ollama("/api/chat", {
            "model": cfg.EXECUTOR_MODEL, "stream": False, "think": False,
            "format": {"type": "object", "properties": {"voice": {"type": "string"}},
                       "required": ["voice"]},
            "messages": [{"role": "system", "content": VOICE_BRIEF_RULES},
                         {"role": "user", "content":
                          f"Story setting (excerpt): {(project['story'] or '')[:700]}\n\n"
                          f"Character: {r['name']}\nDescription: {r['description'] or '(none)'}"}],
            "options": {"num_gpu": 0, "num_thread": cfg.EXECUTOR_THREADS, "num_ctx": 4096,
                        "temperature": 0.3}}, timeout=240)
        voice = (agent._parse_json((res.get("message") or {}).get("content", "")) or {}).get("voice")
        if voice and len(voice) > 20:
            return " ".join(voice.split())[:600]
    except Exception as exc:                                   # noqa: BLE001
        log.warning("voice brief for %s by the CPU model failed: %s", r["name"], exc)
    return _guess_brief(r)


NEUTRAL_SAMPLE = ("Listen closely, for this is how I speak: steady and true. Some words come "
                  "easily, and some must be weighed before they are said.")


def _sample_text(pid: str, r) -> str:
    """What the designed sample says: the character's own lines when they are in English,
    each once, topped up with a neutral line to ~8 s or more (cloning needs that much; a
    5 s sample of one line said twice gave a weak clone, 2026-11-25)."""
    said, total = [], 0
    for c in db.query("SELECT text FROM prod_clips WHERE project_id=? AND ref_id=? AND text "
                      "IS NOT NULL ORDER BY start_s", (pid, r["id"])):
        t = " ".join((c["text"] or "").split())
        if t and t.isascii() and t not in said:
            said.append(t)
            total += len(t)
        if total > 200:
            break
    text = " ".join(said)
    if total < 110:
        text = (text + " " + NEUTRAL_SAMPLE).strip()
    return text[:420]


def _gender_of(brief: str) -> str | None:
    b = (brief or "").lower()
    if re.search(r"\b(female|woman|women|girl|feminine|she|her|queen|lady|mother)\b", b):
        return "female"
    if re.search(r"\b(male|man|men|boy|masculine|he|his|king|father)\b", b):
        return "male"
    return None


def _pitch_mismatch(brief: str, f0: float | None) -> str | None:
    """Why a designed sample does not match its brief, or None."""
    if not f0:
        return None
    g = _gender_of(brief)
    if "newborn" in (brief or "").lower() or "baby" in (brief or "").lower():
        return None
    if g == "female" and f0 < 160:
        return "female"
    if g == "male" and f0 > 175:
        return "male"
    return None


async def design_voice(project, user: auth.User, ref_id: str, brief: str | None = None) -> str:
    """Design this character's voice now (queued on the GPU like any speech). Returns the
    speech id; the reference takes the sample when it is ready (_run_voices)."""
    r = _ref(project["id"], ref_id)
    if r["kind"] not in VOICE_KINDS:
        raise HTTPException(400, "only characters and voices have a voice")
    brief = " ".join((brief or "").split()) or await voice_brief(project, r)
    sid = (await speech.create(user, _sample_text(project["id"], r), design=brief,
                               language="English"))[0]
    db.run("UPDATE prod_refs SET voice_design=?, voice_job=?, voice_auto=1 WHERE id=?",
           (brief[:700], sid, ref_id))
    _touch(project["id"])
    log.info("designing a voice for %s: %s", r["name"], brief[:120])
    return sid


async def _run_voices() -> None:
    """Attach finished designs (and re-voice that character's lines made without them);
    start designs for characters that want one."""
    from app import agent
    for r in db.query("SELECT * FROM prod_refs WHERE voice_job IS NOT NULL AND voice_job != ''"):
        row = db.one("SELECT * FROM speech WHERE id=?", (r["voice_job"],))
        if row is None or row["status"] in ("failed", "cancelled"):
            # Never leave lines waiting forever: a preset of the right gender instead.
            fallback = "Serena" if re.search(r"\b(woman|female|girl|queen|she|her)\b",
                                             (r["voice_design"] or "").lower()) else "Ryan"
            db.run("UPDATE prod_refs SET voice_job=NULL, speaker=? WHERE id=?", (fallback, r["id"]))
            log.warning("voice design for %s failed; using %s", r["name"], fallback)
            continue
        if row["status"] != "succeeded" or not row["path"] or not Path(row["path"]).exists():
            continue
        p = speech.params(row)
        wrong = _pitch_mismatch(r["voice_design"], p.get("f0"))
        tries = int(p.get("try") or 1)
        if wrong and tries < 3:
            # Off for its description (a "female" voice at 128 Hz): design again, more clearly.
            cue = (" Clearly feminine, a higher, lighter female voice." if wrong == "female" else
                   " Clearly masculine, a lower, deeper male voice.")
            owner = agent.load_user(row["user_id"])
            if owner is not None:
                sid = (await speech.create(owner, row["text"], design=(r["voice_design"] + cue)[:700],
                                           language=row["language"], seed=7919 * tries + 13))[0]
                pr = speech.params(db.one("SELECT * FROM speech WHERE id=?", (sid,)))
                pr["try"] = tries + 1
                db.run("UPDATE speech SET params=? WHERE id=?", (json.dumps(pr), sid))
                db.run("UPDATE prod_refs SET voice_job=? WHERE id=?", (sid, r["id"]))
                log.info("voice for %s came out at %.0f Hz for a %s voice; designing again (try %d)",
                         r["name"], p["f0"], wrong, tries + 1)
                continue
        dest = cfg.uploads_dir / r["project_id"] / "voices"
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / f"{r['id']}.mp3"
        shutil.copyfile(row["path"], path)
        db.run("UPDATE prod_refs SET voice_path=?, voice_text=?, voice_job=NULL WHERE id=?",
               (str(path), row["text"], r["id"]))
        # Lines already voiced in another voice are voiced again in the new one.
        again = revoice_lines(r["id"])
        _touch(r["project_id"])
        log.info("voice for %s is ready%s", r["name"],
                 f"; {again} line(s) voiced again in it" if again else "")
    started = 0
    for r in db.query("SELECT r.*, p.user_id AS owner FROM prod_refs r JOIN projects p ON "
                      "p.id=r.project_id WHERE r.voice_auto=1 AND (r.voice_job IS NULL OR "
                      "r.voice_job='') AND (r.voice_path IS NULL OR r.voice_path='') AND "
                      "(r.speaker IS NULL OR r.speaker='') AND r.voice_design != 'off' "
                      "ORDER BY r.created_at LIMIT 6"):
        user = agent.load_user(r["owner"])
        project = db.one("SELECT * FROM projects WHERE id=?", (r["project_id"],))
        if user is None or project is None or r["kind"] not in VOICE_KINDS:
            db.run("UPDATE prod_refs SET voice_auto=0 WHERE id=?", (r["id"],))
            continue
        try:
            await design_voice(project, user, r["id"])
            started += 1
        except HTTPException as exc:
            log.warning("could not design a voice for %s: %s", r["name"], exc.detail)
            db.run("UPDATE prod_refs SET voice_auto=0 WHERE id=?", (r["id"],))


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


def render_when_ready(pid: str, user: auth.User, clip_id: str, shot_id: str) -> None:
    """Render a shot once the dialogue line under it is ready. Saved in the project's
    params (not a bare asyncio task: an unreferenced task can be garbage-collected, and a
    restart would lose it); `pending_loop` carries it out."""
    project = db.one("SELECT * FROM projects WHERE id=?", (pid,))
    params = db.params_of(project)
    waits = [w for w in params.get("_render_after") or [] if w.get("shot") != shot_id]
    waits.append({"shot": shot_id, "clip": clip_id, "user": user.id, "at": db.now()})
    params["_render_after"] = waits
    db.set_params(pid, params)


async def _run_angles() -> None:
    from app import agent
    for r in db.query("SELECT r.*, p.user_id AS owner FROM prod_refs r JOIN projects p ON "
                      "p.id=r.project_id WHERE r.kind IN ('character','location') AND "
                      "r.angles LIKE '%_auto%'"):
        if not r["image_id"]:
            continue
        st = db.one("SELECT status FROM images WHERE id=?", (r["image_id"],))
        if st is None or st["status"] in ("failed", "cancelled"):
            db.run("UPDATE prod_refs SET angles='{}' WHERE id=?", (r["id"],))
            continue
        if st["status"] != "succeeded":
            try:
                await images.refresh(r["image_id"])
            except Exception:                                  # noqa: BLE001
                pass
            continue
        user = agent.load_user(r["owner"])
        project = db.one("SELECT * FROM projects WHERE id=?", (r["project_id"],))
        if user is None or project is None:
            continue
        db.run("UPDATE prod_refs SET angles=? WHERE id=?",
               (json.dumps(_keep_mine(_jl(r["angles"], {}))), r["id"]))
        try:
            await make_angles(project, user, r["id"])
            log.info("turnaround queued for %s", r["name"])
        except HTTPException as exc:
            log.warning("no turnaround for %s: %s", r["name"], exc.detail)


async def _run_scripts() -> None:
    from app import agent
    for c in db.query("SELECT c.id, c.project_id, p.user_id AS owner FROM prod_clips c JOIN projects p "
                      "ON p.id=c.project_id WHERE c.status='script' AND c.auto=1 "
                      # lines waiting for their character's voice are skipped, not counted
                      "AND (c.ref_id IS NULL OR NOT EXISTS (SELECT 1 FROM prod_refs r WHERE "
                      "r.id=c.ref_id AND r.kind IN ('character','voice') AND "
                      "(r.voice_path IS NULL OR r.voice_path='') AND (r.speaker IS NULL OR "
                      "r.speaker='') AND r.voice_design != 'off' AND r.voice_auto=1 AND "
                      "r.voice_job IS NOT NULL)) "
                      "ORDER BY c.project_id, c.start_s LIMIT 12"):
        user = agent.load_user(c["owner"])
        project = db.one("SELECT * FROM projects WHERE id=?", (c["project_id"],))
        if user is None or project is None:
            continue
        try:
            await voice_script(project, user, c["id"])
        except HTTPException as exc:
            # e.g. the hourly speech limit: leave it as a script, say why, stop trying.
            db.run("UPDATE prod_clips SET auto=0, error=? WHERE id=?", (str(exc.detail)[:300], c["id"]))


async def _run_pending() -> None:
    await _run_angles()
    await _run_voices()
    await _run_scripts()
    await _run_auto_shots()
    from app import agent
    for project in db.query("SELECT * FROM projects WHERE mode='storyboard' AND "
                            "params_json LIKE '%_render_after%'"):
        params = db.params_of(project)
        waits = params.get("_render_after") or []
        if not waits:
            continue
        refresh_clips(project["id"])
        keep = []
        for w in waits:
            c = db.one("SELECT status FROM prod_clips WHERE id=?", (w["clip"],))
            shot = db.one("SELECT id FROM shots WHERE id=?", (w["shot"],))
            if c is None or shot is None or c["status"] == "failed" or \
                    db.now() - w.get("at", 0) > 6 * 3600:
                continue                                   # gone or hopeless: drop it
            if c["status"] != "ready" or service.is_rendering(
                    db.one("SELECT * FROM projects WHERE id=?", (project["id"],))):
                keep.append(w)
                continue
            user = agent.load_user(w["user"])
            if user is None:
                continue
            try:
                await render_shots(db.one("SELECT * FROM projects WHERE id=?", (project["id"],)),
                                   user, [w["shot"]])
                log.info("rendering %s now that its line is ready", w["shot"])
            except HTTPException as exc:
                log.warning("could not render %s after its line: %s", w["shot"], exc.detail)
        p2 = db.params_of(db.one("SELECT * FROM projects WHERE id=?", (project["id"],)))
        p2["_render_after"] = keep
        db.set_params(project["id"], p2)


async def pending_loop() -> None:
    while True:
        try:
            await _run_pending()
        except asyncio.CancelledError:
            raise
        except Exception:                                  # noqa: BLE001
            log.exception("editor pending loop survived an error")
        await asyncio.sleep(15)


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


def _fit_lipsync(pid: str, clip_id: str) -> None:
    """A lip-sync shot grows to hold its voiced line (the video is made from that voice)."""
    c = db.one("SELECT start_s, duration_s FROM prod_clips WHERE id=?", (clip_id,))
    if c is None:
        return
    tm = timings(pid)
    for sid, (st, ln) in tm.items():
        if st <= c["start_s"] < st + ln:
            shot = db.one("SELECT make, seconds FROM shots WHERE id=?", (sid,))
            need = math.ceil(c["start_s"] + (c["duration_s"] or 0) - st + 0.3)
            if shot and shot["make"] == "lipsync" and need > shot["seconds"]:
                project = db.one("SELECT * FROM projects WHERE id=?", (pid,))
                if not service.is_rendering(project):
                    edit_shot(project, sid, seconds=min(cfg.LIPSYNC_MAX_SECONDS, need))
            return


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
            if c["track"] == "dialogue":
                _fit_lipsync(pid, c["id"])
        elif row["status"] in ("failed", "cancelled"):
            db.run("UPDATE prod_clips SET status='failed', error=?, updated_at=? WHERE id=?",
                   (row["error"] or row["status"], db.now(), c["id"]))


# --------------------------------------------------------------------------- generation

# A character's turnaround, each drawn FROM its main picture so it stays the same
# character (2026-11-24: "your character reference should generate all angle reference").
ANGLES = {"three_quarter": "three-quarter view, turned 45 degrees",
          "side": "side profile view, facing left",
          "back": "back view",
          "face": "EXTREME CLOSE-UP PORTRAIT: only the head fills the whole frame, no body or "
                  "legs visible, face toward the camera, detailed eyes and expression"}
# A place gets its own set of views (2026-11-24: "location should also have multiple shot
# options"), each drawn from the main picture so it stays the same place.
PLACE_VIEWS = {"wide": "a very wide establishing view of the whole place from much further back",
               "reverse": "the reverse angle: standing at the far side looking back toward where "
                          "the first view was taken",
               "detail": "a macro close-up of a characteristic detail of the place",
               "aerial": "a high aerial view looking straight down over the whole place"}
# FLUX klein copied the main picture's framing for places (four near-identical "views");
# Qwen-Image with the same source gave real new camera positions of the same place
# (measured 2026-11-24 on a meadow: aerial, reverse, detail all distinct and consistent).
PLACE_VIEW_MODEL = "qwen-image-2.1"


def views_for(kind: str) -> dict:
    return ANGLES if kind == "character" else PLACE_VIEWS if kind == "location" else {}


def _angle_done(r) -> dict[str, str]:
    """{angle: image_id} for the finished angle pictures of a reference."""
    out = {}
    for k, i in _jl(r["angles"], {}).items():
        if isinstance(i, str) and not k.startswith("_") and _img_done(i):
            out[k] = i
    return out


# The person's own photos (2026-11-24: "allow user to upload multiple photos if you are
# unable to generate multiple angle photos from 1 pic"). In the angles map:
#   a view key ("side", "wide", ...) -> an uploaded photo for that view, listed in _mine;
#   "photo_N"                        -> extra photos, also in _mine;
#   "_skip": views the person removed -- never redrawn automatically.
# Generated views never overwrite anything in _mine.
MAX_EXTRA_PHOTOS = 6


def _marks(angles: dict) -> tuple[set, set]:
    return set(angles.get("_mine") or []), set(angles.get("_skip") or [])


def _keep_mine(angles: dict) -> dict:
    """What survives a redraw: the person's photos and their removals."""
    mine, skip = _marks(angles)
    out = {k: v for k, v in angles.items() if k in mine}
    if mine:
        out["_mine"] = sorted(mine)
    if skip:
        out["_skip"] = sorted(skip)
    return out


def add_ref_photos(project, user: auth.User, ref_id: str, files: list[tuple[str, bytes]],
                   slot: str | None = None) -> dict:
    pid = project["id"]
    r = _ref(pid, ref_id)
    views = views_for(r["kind"])
    angles = {k: v for k, v in _jl(r["angles"], {}).items() if k != "_auto"}
    mine, skip = _marks(angles)
    ids = [_import_picture(user, name, raw) for name, raw in files if raw]
    if not ids:
        raise HTTPException(400, "no photo")
    placed = []
    if slot:
        if slot == "main":
            db.run("UPDATE prod_refs SET image_id=? WHERE id=?", (ids[0], ref_id))
            placed.append("main")
        elif slot in views or (slot.startswith("photo_") and slot in angles):
            angles[slot] = ids[0]
            mine.add(slot)
            skip.discard(slot)
            placed.append(slot)
        else:
            raise HTTPException(400, "no such view")
    else:
        queue = list(ids)
        if not (r["image_id"] and _img_done(r["image_id"])):
            db.run("UPDATE prod_refs SET image_id=? WHERE id=?", (queue.pop(0), ref_id))
            placed.append("main")
        n = 1
        for i in queue:
            while f"photo_{n}" in angles:
                n += 1
            if n > MAX_EXTRA_PHOTOS:
                break
            angles[f"photo_{n}"] = i
            mine.add(f"photo_{n}")
            placed.append(f"photo_{n}")
    angles["_mine"] = sorted(mine)
    if skip:
        angles["_skip"] = sorted(skip)
    db.run("UPDATE prod_refs SET angles=? WHERE id=?", (json.dumps(angles), ref_id))
    _touch(pid)
    return {"placed": placed, "dropped": len(ids) - len(placed)}


def remove_ref_view(project, ref_id: str, key: str) -> None:
    pid = project["id"]
    r = _ref(pid, ref_id)
    angles = _jl(r["angles"], {})
    mine, skip = _marks(angles)
    angles.pop(key, None)
    mine.discard(key)
    if key in views_for(r["kind"]):
        skip.add(key)                     # removed on purpose: do not draw it again by itself
    angles["_mine"], angles["_skip"] = sorted(mine), sorted(skip)
    db.run("UPDATE prod_refs SET angles=? WHERE id=?", (json.dumps(angles), ref_id))
    _touch(pid)


async def make_angles(project, user: auth.User, ref_id: str, only: list | None = None,
                      model: str | None = None) -> dict:
    """Queue the turnaround pictures (needs a finished main picture). `model`: the person's
    choice when the default draws them wrong; otherwise klein for characters and
    Qwen-Image for places."""
    pid = project["id"]
    r = _ref(pid, ref_id)
    if not r["image_id"] or not _img_done(r["image_id"]):
        raise HTTPException(400, f"{r['name']} needs its main picture first")
    src = [images.source_from_image(user, r["image_id"])]
    for i in _own_photos(r)[:3]:                 # the person's photos say more than one picture
        try:
            src.append(images.source_from_image(user, i))
        except HTTPException:
            pass
    src = src[:images.MODEL_MAX_SOURCES.get(model or "", images.MAX_SOURCES)]
    more = (f" Pictures 2-{len(src)} are more photos of the same "
            f"{'place' if r['kind'] == 'location' else 'character'}." if len(src) > 1 else "")
    params = db.params_of(project)
    angles = {k: v for k, v in _jl(r["angles"], {}).items() if k != "_auto"}
    views = views_for(r["kind"])
    if not views:
        raise HTTPException(400, f"{r['kind']} references have no extra views")
    mine, skip = _marks(angles)
    if only:                         # the person asked for these: redraw even their own
        keys = [k for k in only if k in views]
        mine -= set(keys)
        skip -= set(keys)
    else:                            # all: everything except the person's photos
        keys = [k for k in views if k not in mine]
        skip = set()
    angles["_mine"], angles["_skip"] = sorted(mine), sorted(skip)
    for key in keys:
        if r["kind"] == "location":
            text = (f"Same place as picture 1 ({r['name']}), but {views[key]}. Keep the same "
                    "landmarks, colours, light and style. No characters.")
        elif key == "face":
            text = (f"{ANGLES[key]}. The same character as in picture 1 ({r['name']}): exactly "
                    "the same face, colours and markings. Plain soft background.")
        else:
            text = (f"The same character as in picture 1: {r['name']}, {r['description']}. "
                    f"{views[key]}. Exactly the same design, outfit, colours and proportions; "
                    "plain soft background; character turnaround reference.")
        angles[key] = await images.create(user, (text + more)[:1900], src,
                                          orientation=params.get("orientation", "portrait"),
                                          model=model or (PLACE_VIEW_MODEL if r["kind"] == "location"
                                                          else None))
    db.run("UPDATE prod_refs SET angles=? WHERE id=?", (json.dumps(angles), ref_id))
    _touch(pid)
    return angles


def _want_angles(pid: str, ref_id: str) -> None:
    """Mark a character or place for its views once its main picture is ready (pending_loop)."""
    r = db.one("SELECT angles FROM prod_refs WHERE id=?", (ref_id,))
    keep = _keep_mine(_jl(r["angles"] if r else "{}", {}))
    db.run("UPDATE prod_refs SET angles=? WHERE id=? AND kind IN ('character','location')",
           (json.dumps({**keep, "_auto": True}), ref_id))


def _own_photos(r) -> list[str]:
    """The person's own photos of a reference (views they replaced, then extra photos)."""
    angles = _jl(r["angles"], {})
    mine, _ = _marks(angles)
    done = _angle_done(r)
    order = [k for k in views_for(r["kind"]) if k in mine] + \
        sorted(k for k in mine if k.startswith("photo_"))
    return [done[k] for k in order if k in done]


def _ref_sources(user: auth.User, pid: str, ref_ids: list,
                 limit: int = 4) -> tuple[list[tuple[str, bytes]], str]:
    """Source pictures for a shot's picture, and the sentence naming them.

    WIS takes 4. Every reference's main picture first; then the person's own photos
    (2026-11-24: "the system should use the multiple reference photos (if uploaded) to
    generate better images"), one reference at a time; then a drawn three-quarter view."""
    refs = [r for r in (db.one("SELECT * FROM prod_refs WHERE id=? AND project_id=?", (rid, pid))
                        for rid in ref_ids) if r and r["image_id"] and _img_done(r["image_id"])]
    rounds = [[(r, r["image_id"]) for r in refs]]
    photos = [_own_photos(r) for r in refs]
    for i in range(max((len(p) for p in photos), default=0)):
        rounds.append([(r, p[i]) for r, p in zip(refs, photos) if i < len(p)])
    rounds.append([(r, _angle_done(r)["three_quarter"]) for r in refs
                   if "three_quarter" in _angle_done(r)])
    sources, who = [], {}
    for rnd in rounds:
        for r, image_id in rnd:
            if len(sources) >= limit:
                break
            try:
                sources.append(images.source_from_image(user, image_id))
            except HTTPException:
                continue
            who.setdefault(r["name"], []).append(len(sources))
    text = " ".join(f"{name} looks exactly like picture {nums[0]}." if len(nums) == 1 else
                    f"{name} looks exactly like pictures {', '.join(map(str, nums[:-1]))} and "
                    f"{nums[-1]} (the same {('place' if any(r['name'] == name and r['kind'] == 'location' for r in refs) else 'character')} in each)."
                    for name, nums in who.items())
    return sources, text


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
                          count: int = 2, model: str | None = None,
                          check: bool = True) -> list[str]:
    """Pictures for a shot, made with its references as sources so characters stay
    the same people. The person picks one (or the assistant does)."""
    pid = project["id"]
    shot = _shot(pid, shot_id)
    count = max(1, min(4, int(count or 2)))
    if shot["make"] in REMAKE_MAKES and not prompt.strip():
        return await remake_picture_options(project, user, shot, count)
    from app import shotpics
    plan = None
    if not prompt.strip():
        # The local planner writes the picture as a STILL and says who is in it (shotpics).
        plan = shotpics.cached(pid, shot)
        if plan is None and cfg.PROMPT_POLISH:
            try:
                await shotpics.plan_many(pid, [shot_id])
            except Exception as exc:                           # noqa: BLE001
                log.warning("picture plan for %s: %s", shot_id, exc)
            plan = shotpics.cached(pid, _shot(pid, shot_id))
    framing = {"EWS": "extreme wide shot", "WS": "wide shot", "MS": "medium shot",
               "MCU": "medium close-up", "CU": "close-up", "ECU": "extreme close-up",
               "OTS": "over-the-shoulder shot", "POV": "point-of-view shot",
               "2S": "two-shot", "INSERT": "insert shot"}.get(shot["framing"], "")
    text = prompt.strip() or (plan or {}).get("prompt") or " ".join(
        x for x in (framing, shot["prompt"], _shot_context(pid, shot)) if x)
    if not text:
        raise HTTPException(400, "describe the picture, or give the shot a title and prompt")
    sources, who = shotpics.sources_for(user, pid, shot, plan, text)
    if who:
        text += " " + who
    params = db.params_of(project)
    ids = []
    for i in range(count):
        ids.append(await images.create(user, text[:1900], sources,
                                       orientation=params.get("orientation", "portrait"),
                                       model=shotpics.option_model(i, model), seed=shotpics.seed()))
    if check and cfg.PROMPT_POLISH:
        asyncio.create_task(_check_options_later(pid, shot_id, ids))
    return ids


async def _check_options_later(pid: str, shot_id: str, ids: list[str]) -> None:
    """When the options are drawn, the planner looks at them; its verdict goes on the
    proposal ("⭐ option 2 fits best ...") so the person sees it beside the pictures."""
    from app import shotpics
    try:
        for i in ids:
            await images.wait(i, 600)
        res = await shotpics.judge(pid, _shot(pid, shot_id), ids)
        if not res:
            return
        n = ids.index(res["best"]) + 1
        bits = [f"⭐ The assistant's pick: option {n} ({res['scores'][res['best']]:.0f}/10)"]
        for k, i in enumerate(ids):
            if i != res["best"] and res["fails"].get(i):
                bits.append(f"option {k + 1}: {'; '.join(res['fails'][i][:2])}")
        prop = db.one("SELECT id FROM proposals WHERE project_id=? AND options LIKE ?",
                      (pid, f"%{ids[0]}%"))
        if prop:
            db.run("UPDATE proposals SET summary=? WHERE id=?", (" · ".join(bits)[:500], prop["id"]))
            _touch(pid)
    except Exception as exc:                                   # noqa: BLE001
        log.warning("checking picture options for %s: %s", shot_id, exc)


CONTINUITY_RULES = """You are a film editor. For each shot after the first, decide whether it is
ONE CONTINUOUS TAKE with the shot before it: the camera keeps rolling and the new shot starts
exactly where the previous one ended (same place, same moment, the action carries straight on,
so its first frame should be the previous shot's last frame). A cut to another angle, framing,
character, place or moment is NOT continuous. Most shots are cuts; say continuous only when the
story clearly flows on without a cut. Answer JSON only."""


async def check_continuity(pid: str, scene_id: str) -> dict:
    """Decide (on the CPU model) which shots of a scene continue the previous shot.
    Shots the person or the planner already decided keep their choice."""
    from app import agent
    shots = [s for s in ordered_shots(pid) if s["scene_id"] == scene_id]
    todo = [i for i, s in enumerate(shots) if i > 0 and not s["cont_checked"]]
    if shots and not shots[0]["cont_checked"]:
        db.run("UPDATE shots SET cont_checked=1, start_from='' WHERE id=?", (shots[0]["id"],))
    if not todo:
        return {}
    scene = _scene(pid, scene_id)
    lines = [f"{i + 1}. [{s['framing'] or '?'} {s['seconds']}s] {s['title']}: {(s['prompt'] or '')[:300]}"
             for i, s in enumerate(shots)]
    ask = (f"Scene: {scene['title']} — {scene['summary'][:600]}\nShots in order:\n" +
           "\n".join(lines) + "\n\nFor shots " + ", ".join(str(i + 1) for i in todo) +
           " answer {\"shots\": [{\"n\": <number>, \"continuous\": true|false, \"why\": \"...\"}]}")
    decided = {}
    try:
        res = await agent._ollama("/api/chat", {
            "model": cfg.EXECUTOR_MODEL, "stream": False, "think": False, "keep_alive": "2m",
            "format": {"type": "object", "properties": {"shots": {"type": "array", "items": {
                "type": "object", "properties": {"n": {"type": "integer"},
                                                 "continuous": {"type": "boolean"},
                                                 "why": {"type": "string"}},
                "required": ["n", "continuous"]}}}, "required": ["shots"]},
            "messages": [{"role": "system", "content": CONTINUITY_RULES},
                         {"role": "user", "content": ask}],
            "options": {"num_gpu": 0, "num_thread": cfg.EXECUTOR_THREADS, "num_ctx": 8192,
                        "temperature": 0.1}}, timeout=300)
        for d in (agent._parse_json((res.get("message") or {}).get("content", "")) or {}).get("shots", []):
            n = int(d.get("n", 0)) - 1
            if n in todo:
                decided[shots[n]["id"]] = bool(d.get("continuous"))
    except Exception as exc:                                   # noqa: BLE001
        log.warning("continuity check for %s failed (%s); treating shots as cuts", scene_id, exc)
    for i in todo:
        sid = shots[i]["id"]
        cont = decided.get(sid, False)
        db.run("UPDATE shots SET cont_checked=1, start_from=? WHERE id=?",
               ("prev_last" if cont else "", sid))
    log.info("continuity %s: %s", scene["title"], {shots[i]["title"]: decided.get(shots[i]["id"], False)
                                                   for i in todo})
    _touch(pid)
    return decided


def _shot_ref_ids(shot) -> list[str]:
    """A shot's references plus its scene's (the scene group's cast and set)."""
    ids = list(_jl(shot["refs"], []))
    if shot["scene_id"]:
        row = db.one("SELECT refs FROM prod_scenes WHERE id=?", (shot["scene_id"],))
        ids += [r for r in _jl(row["refs"] if row else "[]", []) if r not in ids]
    return ids


def _ref_ready(r) -> str:
    """'ready', 'waiting' (being drawn), or 'missing' (picture or views not made)."""
    if not r["image_id"]:
        return "missing"
    row = db.one("SELECT status FROM images WHERE id=?", (r["image_id"],))
    if row is None or row["status"] in ("failed", "cancelled"):
        return "missing"
    if row["status"] != "succeeded":
        return "waiting"
    views = views_for(r["kind"])
    if not views:
        return "ready"
    angles = _jl(r["angles"], {})
    if angles.get("_auto"):
        return "waiting"
    _, skip = _marks(angles)
    states = [(db.one("SELECT status FROM images WHERE id=?", (angles[k],)) or {"status": "failed"})["status"]
              if isinstance(angles.get(k), str) else "missing" for k in views if k not in skip]
    if any(st not in images.TERMINAL and st != "missing" for st in states):
        return "waiting"
    return "ready" if all(st == "succeeded" for st in states) else "missing"


def prepare_render(project, user: auth.User, shot_ids: list[str] | None,
                   step: str = "videos") -> tuple[list, list]:
    """Rendering is two separate steps, each started by the person (2026-11-24: "Render
    missing" drew the pictures and then went straight on to the video model, and the
    person had to press Stop to look at the pictures first).

    step="pictures": (now=[], later) -- `later` shots go through continuity -> their
      characters' and places' pictures and views -> the shot's picture, in the background
      (`_run_auto_shots`), and STOP there. No video model.
    step="videos": (now, skipped) -- render the shots that have a picture (or open on the
      previous shot's last frame) now; `skipped` still need a picture. Nothing is drawn.
    """
    pid = project["id"]
    shots = ordered_shots(pid)
    if step == "videos":
        return _ready_for_video(shots, shot_ids)
    want = list(shot_ids or []) or [s["id"] for s in shots
                                    if s["status"] != "done" or not (s["clip_path"] and
                                                                     Path(s["clip_path"]).exists())]
    by_id = {s["id"]: s for s in shots}
    now, later = [], []
    unchecked_scenes = {by_id[sid]["scene_id"] for sid in want if sid in by_id
                        and not by_id[sid]["cont_checked"]}
    refs_needed = set()
    for sid in want:
        if sid in by_id:
            for rid in _shot_ref_ids(by_id[sid]):
                r = db.one("SELECT * FROM prod_refs WHERE id=?", (rid,))
                if r is not None and _ref_ready(r) != "ready":
                    refs_needed.add(sid)
                    break
    for sid in want:
        s = by_id.get(sid)
        if s is None:
            continue
        # Continuity first: a shot in a scene not yet checked waits for the check; a
        # shot without a picture (that is not continuous) waits for its picture.
        if s["scene_id"] in unchecked_scenes or sid in refs_needed or \
                (not s["asset_id"] and s["start_from"] != "prev_last"):
            later.append(sid)
        else:
            now.append(sid)
    # A continuous shot after a shot that waits for its picture has to wait with it.
    for sid in list(now):
        s = by_id[sid]
        i = [x["id"] for x in shots].index(sid)
        if s["start_from"] == "prev_last" and i > 0 and shots[i - 1]["id"] in later:
            now.remove(sid)
            later.append(sid)
    # Pictures only: shots that already have their picture and references are done, and
    # shots whose action comes from a video need none (unless asked for by name).
    later = [sid for sid in later if (not by_id[sid]["asset_id"] or sid in refs_needed
                                      or by_id[sid]["scene_id"] in unchecked_scenes)
             and (shot_ids or needs_picture(pid, by_id[sid]))]
    if later:
        params = db.params_of(project)
        jobs = [j for j in params.get("_auto_shots") or [] if j["shot"] not in later]
        jobs += [{"shot": sid, "user": user.id, "stage": "continuity", "at": db.now(),
                  "until": "picture"} for sid in later]
        params["_auto_shots"] = jobs
        db.set_params(pid, params)
    return [], later


def _agent_shot_extras(pid: str, s) -> str:
    """The use-case settings of a shot, for the assistant's view of the production."""
    out = []
    vids = _ref_video_items(pid, s)
    if vids:
        out.append("ref_videos=[" + "; ".join(label for label, _ in vids) + "]")
    o = _jl(s["opts"], {})
    if s["make"] in TALK_MAKES:
        sp = [x.get("clip") or "none" for x in (o.get("speakers") or [])]
        out.append(f"voices=[{', '.join(sp) or 'none yet'}]"
                   + (f" first-speaker={'right' if o.get('layout') == 'right_left' else 'left'}"
                      if s["make"] == "talk2" else ""))
    if s["make"] == "recast":
        out.append("cast=[" + "; ".join(f"{c['ref']} replaces '{c.get('who')}'"
                                        for c in o.get("cast") or []) + "]")
    if s["make"] == "restyle":
        out.append(f"change='{(o.get('change') or '')[:160]}'")
    return (" ".join(out) + " ") if out else ""


def needs_picture(pid: str, shot) -> bool:
    """Whether a shot's video is made FROM its own starting picture.

    2026-11-24, the person: "if there is an action from reference video, why are we asking
    for the starting image?" It isn't needed when the action comes from elsewhere:
      recast / restyle -- the video; the painted frame is made during the render if absent
      motion           -- the video; the character's own picture says who performs
      perform          -- the reference model has no first frame; references are enough
    """
    make = shot["make"] or "animate"
    if make in REMAKE_MAKES:
        return False
    if make == "motion":
        return _performer_picture(pid, shot) is None
    if make == "perform":
        return not (_shot_ref_ids(shot) or _jl(shot["ref_videos"], []))
    return True


def _performer_picture(pid: str, shot) -> str | None:
    """A motion shot without a picture: the first character in it (or its scene) performs,
    as drawn in their main picture."""
    for rid in _shot_ref_ids(shot):
        r = db.one("SELECT * FROM prod_refs WHERE id=? AND project_id=?", (rid, pid))
        if r and r["kind"] == "character" and r["image_id"] and _img_done(r["image_id"]):
            row = db.one("SELECT path FROM images WHERE id=?", (r["image_id"],))
            if row and row["path"] and Path(row["path"]).exists():
                return row["path"]
    return None


async def ensure_painted(project, shot_id: str) -> str | None:
    """A recast/restyle shot rendering without its painted frame: make one now and set it
    as the shot's picture (so the person sees what the remake was drawn from)."""
    from app import agent
    pid = project["id"]
    shot = _shot(pid, shot_id)
    if shot["asset_id"]:
        return None
    user = agent.load_user(project["user_id"])
    ids = await remake_picture_options(project, user, shot, 1)
    row = await images.wait(ids[0], 300)
    if row["status"] != "succeeded":
        raise RuntimeError(f"{shot['title'] or 'this shot'}: the painted frame could not be "
                           f"made ({row['error'] or row['status']})")
    set_picture(project, user, shot_id, ids[0])
    return _shot(pid, shot_id)["asset_id"]


def _ready_for_video(shots, shot_ids: list[str] | None) -> tuple[list, list]:
    """(shots that can go to the video model now, shots that still need a picture)."""
    want = list(shot_ids or []) or [s["id"] for s in shots
                                    if s["status"] != "done" or not (s["clip_path"] and
                                                                     Path(s["clip_path"]).exists())]
    order = [s["id"] for s in shots]
    by_id = {s["id"]: s for s in shots}
    now, skipped = [], []
    for sid in want:
        s = by_id.get(sid)
        if s is None:
            continue
        ok = bool(s["asset_id"]) or not needs_picture(s["project_id"], s)
        if s["start_from"] == "prev_last":
            # Opens on the previous clip: fine if that clip exists or renders in this batch.
            i = order.index(sid)
            prev = by_id[order[i - 1]] if i > 0 else None
            ok = prev is not None and (prev["id"] in now or (
                prev["clip_path"] and Path(prev["clip_path"]).exists()))
        (now if ok else skipped).append(sid)
    return now, skipped


async def _run_auto_shots() -> None:
    """Advance shots waiting for a picture: references -> shot picture -> render."""
    from app import agent
    for project in db.query("SELECT * FROM projects WHERE mode='storyboard' AND "
                            "params_json LIKE '%_auto_shots%'"):
        pid = project["id"]
        jobs = db.params_of(project).get("_auto_shots") or []
        if not jobs:
            continue
        keep, to_render = [], {}
        checked: set = set()
        drawing: set = set()
        if cfg.PROMPT_POLISH:
            # Plan every waiting shot's picture in ONE turn of the planner model, rather
            # than loading it once per shot (shotpics.plan_many skips planned ones).
            need = [j["shot"] for j in jobs if (row := db.one(
                "SELECT asset_id, make, start_from FROM shots WHERE id=?", (j["shot"],)))
                and not row["asset_id"] and row["make"] not in REMAKE_MAKES
                and row["start_from"] != "prev_last"]
            if need:
                from app import shotpics
                try:
                    await shotpics.plan_many(pid, need)
                except Exception as exc:                       # noqa: BLE001
                    log.warning("planning pictures for %s: %s", pid, exc)
        for j in jobs:
            shot = db.one("SELECT * FROM shots WHERE id=?", (j["shot"],))
            user = agent.load_user(j["user"])
            if shot is None or user is None or db.now() - j.get("at", 0) > 12 * 3600:
                continue
            proj = db.one("SELECT * FROM projects WHERE id=?", (pid,))
            try:
                if j["stage"] == "continuity":
                    if not shot["cont_checked"] and shot["scene_id"] not in checked:
                        checked.add(shot["scene_id"])
                        await check_continuity(pid, shot["scene_id"])
                    shot = db.one("SELECT * FROM shots WHERE id=?", (j["shot"],))
                    if shot["cont_checked"]:
                        j["stage"] = "refs"
                if j["stage"] == "refs" and shot["start_from"] == "prev_last":
                    j["stage"] = "render"         # opens on the previous clip: no picture needed
                if j["stage"] == "refs":
                    # Every character and place in the shot or its scene: main picture,
                    # then its views, before the shot's own picture or render.
                    waiting = False
                    for rid in _shot_ref_ids(shot):
                        r = db.one("SELECT * FROM prod_refs WHERE id=?", (rid,))
                        if r is None:
                            continue
                        state = _ref_ready(r)
                        if state == "ready":
                            continue
                        waiting = True
                        if not r["image_id"] or (db.one("SELECT status FROM images WHERE id=?",
                                                        (r["image_id"],)) or {"status": "failed"})[
                                "status"] in ("failed", "cancelled"):
                            if rid not in drawing:
                                drawing.add(rid)
                                await make_ref_picture(proj, user, rid)   # views follow
                        elif _img_done(r["image_id"]) and state == "missing" and rid not in drawing:
                            drawing.add(rid)
                            _want_angles(pid, rid)                        # views only
                        elif not _img_done(r["image_id"]):
                            await images.refresh(r["image_id"])
                    if not waiting:
                        j["stage"] = "picture" if not shot["asset_id"] else "render"
                if j["stage"] == "picture" and shot["asset_id"]:
                    j["stage"] = "render"                  # the person set one meanwhile
                elif j["stage"] == "picture" and not j.get("images"):
                    # Two options from two models, the planner picks (shotpics).
                    j["images"] = await picture_options(proj, user, shot["id"], "", 2, check=False)
                elif j["stage"] == "picture":
                    rows = [await images.refresh(i) for i in j["images"]]
                    if any(r["status"] not in images.TERMINAL for r in rows):
                        pass                                    # still drawing
                    elif not any(r["status"] == "succeeded" for r in rows):
                        log.warning("pictures for %s failed; dropped", shot["id"])
                        continue
                    else:
                        from app import shotpics
                        res = await shotpics.judge(pid, shot, j["images"]) if cfg.PROMPT_POLISH else None
                        best = res["best"] if res else next(r["id"] for r in rows
                                                            if r["status"] == "succeeded")
                        if res and res["scores"][best] < shotpics.PASS_SCORE and not j.get("retried"):
                            # Neither fits: two more, and the best of all four is kept.
                            j["retried"] = 1
                            j["images"] = j["images"] + await picture_options(
                                proj, user, shot["id"], "", 2, check=False)
                        else:
                            set_picture(proj, user, shot["id"], best)
                            log.info("picture for %s: %s%s", shot["id"], best,
                                     f" ({res['scores'][best]:.0f}/10)" if res else "")
                            j["stage"] = "render"
                if j["stage"] == "render":
                    if j.get("until") == "picture":
                        continue                   # step 1 only: the person starts the videos
                    to_render.setdefault(j["user"], []).append(shot["id"])
                    continue
            except HTTPException as exc:
                log.warning("auto shot %s: %s", j["shot"], exc.detail)
                continue
            keep.append(j)
        # A continuous shot renders with (or after) the shot it continues: hold it while
        # that one is still waiting for its picture or its continuity check.
        waiting = {j["shot"] for j in keep}
        order = [s["id"] for s in ordered_shots(pid)]
        for uid in list(to_render):
            for sid in list(to_render[uid]):
                row = db.one("SELECT start_from FROM shots WHERE id=?", (sid,))
                i = order.index(sid) if sid in order else 0
                prev = order[i - 1] if i > 0 else None
                if row and row["start_from"] == "prev_last" and prev in waiting:
                    to_render[uid].remove(sid)
                    keep.append({"shot": sid, "user": uid, "stage": "render", "at": db.now()})
                    waiting.add(sid)
            # ...and if the shot it continues has no clip and is not in this batch, it
            # renders first, in the same pass.
            for sid in list(to_render[uid]):
                row = db.one("SELECT start_from FROM shots WHERE id=?", (sid,))
                i = order.index(sid) if sid in order else 0
                while row and row["start_from"] == "prev_last" and i > 0:
                    prev = db.one("SELECT id, clip_path, start_from FROM shots WHERE id=?",
                                  (order[i - 1],))
                    if prev is None or prev["id"] in to_render[uid] or \
                            (prev["clip_path"] and Path(prev["clip_path"]).exists()):
                        break
                    to_render[uid].append(prev["id"])
                    row, i = prev, i - 1
            to_render[uid].sort(key=lambda x: order.index(x) if x in order else 0)
            if not to_render[uid]:
                del to_render[uid]
        proj = db.one("SELECT * FROM projects WHERE id=?", (pid,))
        for uid, ids in to_render.items():
            if service.is_rendering(proj):
                keep += [{"shot": i, "user": uid, "stage": "render", "at": db.now()} for i in ids]
                continue
            try:
                await render_shots(proj, agent.load_user(uid), ids)
                log.info("auto-rendering %d shot(s) in %s", len(ids), pid)
            except HTTPException as exc:
                log.warning("auto render in %s: %s", pid, exc.detail)
            proj = db.one("SELECT * FROM projects WHERE id=?", (pid,))
        p2 = db.params_of(db.one("SELECT * FROM projects WHERE id=?", (pid,)))
        p2["_auto_shots"] = keep
        db.set_params(pid, p2)


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
    want = list(shot_ids or []) or [s["id"] for s in shots
                        if s["status"] != "done" or not (s["clip_path"] and
                                                         Path(s["clip_path"]).exists())]
    if not want:
        raise HTTPException(400, "every shot already has a clip; pick the ones to redo")
    by_id = {s["id"]: s for s in shots}
    pos = {s["id"]: i for i, s in enumerate(shots)}
    tm = timings(pid)
    deferred = []
    for sid in list(want):
        s = by_id.get(sid)
        if s is None or s["make"] != "lipsync" or _dialogue_under(pid, sid):
            continue
        st, ln = tm.get(sid, (0, 0))
        waiting = [c for c in db.query("SELECT id, start_s FROM prod_clips WHERE project_id=? AND "
                                       "track='dialogue' AND status IN ('script','pending')", (pid,))
                   if st <= c["start_s"] < st + ln]
        if waiting:
            # Voice first, then the video: this shot renders once its line is voiced.
            db.run("UPDATE prod_clips SET auto=1 WHERE id=? AND status='script'", (waiting[-1]["id"],))
            render_when_ready(pid, user, waiting[-1]["id"], sid)
            want.remove(sid)
            deferred.append(sid)
    if not want:
        async def _nothing():
            return 0
        return _nothing()
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
        elif not s["asset_id"] and needs_picture(pid, s):
            problems.append(f"{label} has no picture")
        if not (s["prompt"] or "").strip() and s["make"] not in ("lipsync", "motion") + TALK_MAKES + REMAKE_MAKES:
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


# ------------------------------------------------------------ talk + remake shots
# 2026-11-24, "We should use different models for different use cases":
#   talk2    two people in the picture talk, each with their own voice (InfiniteTalk)
#   longtalk one person talks or sings for longer, up to a minute (InfiniteTalk)
#   recast   the people in a video are recast as our characters (Viggle Remake swap)
#   restyle  a video's look is changed: outfits, weather, setting, style (Viggle edit)
# opts: {"speakers": [{"clip": cid, "ref": rid}], "layout": "left_right"|"right_left",
#        "cast": [{"ref": rid, "who": "the man in the grey jacket"}], "change": "..."}
# A speaker's voice is always a clip on the timeline (an upload is added to the
# Dialogue lane first), so every voice can be seen and mixed, and the shot's own audio
# is never mixed on top of it (as with lip-sync).

def _audio_mime(p: Path) -> str:
    return {".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg",
            ".m4a": "audio/mp4"}.get(p.suffix.lower(), "audio/wav")


def _clean_opts(pid: str, o: dict) -> dict:
    out = {}
    sp = []
    for s in (o.get("speakers") or [])[:2]:
        if not isinstance(s, dict):
            continue
        clip = s.get("clip") if s.get("clip") and db.one(
            "SELECT 1 FROM prod_clips WHERE id=? AND project_id=?", (s.get("clip"), pid)) else None
        ref = s.get("ref") if s.get("ref") and db.one(
            "SELECT 1 FROM prod_refs WHERE id=? AND project_id=?", (s.get("ref"), pid)) else None
        sp.append({"clip": clip, "ref": ref})
    if sp:
        out["speakers"] = sp
    if o.get("layout") in ("left_right", "right_left"):
        out["layout"] = o["layout"]
    cast = []
    for c in (o.get("cast") or [])[:3]:
        if isinstance(c, dict) and c.get("ref") and db.one(
                "SELECT 1 FROM prod_refs WHERE id=? AND project_id=?", (c["ref"], pid)):
            cast.append({"ref": c["ref"], "who": str(c.get("who") or "")[:200]})
    if cast:
        out["cast"] = cast
    if o.get("change"):
        out["change"] = str(o["change"])[:800]
    if o.get("who"):
        out["who"] = str(o["who"])[:200]          # MCP recast: who the picture's person replaces
    return out


def _speaker_audio(pid: str, shot) -> list[Path]:
    paths = []
    for s in _jl(shot["opts"], {}).get("speakers") or []:
        row = db.one("SELECT path, status FROM prod_clips WHERE id=? AND project_id=?",
                     (s.get("clip"), pid)) if s.get("clip") else None
        if row and row["status"] == "ready" and row["path"] and Path(row["path"]).exists():
            paths.append(Path(row["path"]))
    return paths


def _sync_talk_seconds(pid: str, shot_id: str) -> None:
    """A talk shot lasts as long as its voices (one after the other)."""
    shot = _shot(pid, shot_id)
    if shot["make"] not in TALK_MAKES:
        return
    total = sum(_probe_seconds(str(p)) or 0 for p in _speaker_audio(pid, shot))
    if total:
        db.run("UPDATE shots SET seconds=? WHERE id=?",
               (max(1, min(cfg.TALK_MAX_SECONDS, int(round(total + 0.3)))), shot_id))


def set_speaker_audio(project, shot_id: str, index: int, name: str, raw: bytes) -> dict:
    """An uploaded voice for speaker `index`: it goes on the Dialogue lane at its place
    in the shot (speaker 2 after speaker 1), then the shot uses that clip."""
    pid = project["id"]
    shot = _shot(pid, shot_id)
    if shot["make"] not in TALK_MAKES:
        raise HTTPException(400, "voices are for “two people talk” and “talk or sing at length” shots")
    opts = _jl(shot["opts"], {})
    sp = (opts.get("speakers") or []) + [{}] * 2
    start = timings(pid).get(shot_id, (0.0, 0.0))[0]
    if index == 1:
        first = _speaker_audio(pid, shot)[:1]
        start += (_probe_seconds(str(first[0])) or 0.0) if first else 0.0
    cid = add_upload(project, "dialogue", name, raw, start)
    sp[index] = {**(sp[index] or {}), "clip": cid}
    opts["speakers"] = sp[:2 if shot["make"] == "talk2" else 1]
    db.run("UPDATE shots SET opts=?, status=CASE WHEN status='done' THEN 'stale' ELSE status END "
           "WHERE id=?", (json.dumps(_clean_opts(pid, opts)), shot_id))
    _sync_talk_seconds(pid, shot_id)
    _touch(pid)
    return {"ok": True, "clip_id": cid}


def _identity_pictures(r) -> list[tuple[str, str]]:
    """(label, image_id) that show who a character is, best first: a face close-up (their
    own, else the drawn one), their own photos, then the main picture. 2026-11-24: a
    recast drawn from the main picture alone came out as a stranger; adding the face
    close-up to the painted frame and to the remake's references fixed it."""
    done = _angle_done(r)
    mine, _ = _marks(_jl(r["angles"], {}))
    out: list[tuple[str, str]] = []
    if "face" in done:
        out.append((f"a close-up photo of {r['name']}'s face", done["face"]))
    for i in _own_photos(r):
        if i not in [x[1] for x in out]:
            out.append((f"a photo of {r['name']}", i))
    if r["image_id"] and _img_done(r["image_id"]) and r["image_id"] not in [x[1] for x in out]:
        out.append((f"a photo of {r['name']}", r["image_id"]))
    return out


def _recast_extras(pid: str, shot, limit: int = 3) -> list[tuple[int, str, str]]:
    """(subject number, label, image path) for the remake's extra references: each cast
    member's identity pictures, taken in turns, up to `limit` (Viggle takes 4 pictures,
    the painted frame being the first)."""
    lists = []
    for n, c in enumerate(_jl(shot["opts"], {}).get("cast") or [], start=1):
        r = db.one("SELECT * FROM prod_refs WHERE id=? AND project_id=?", (c["ref"], pid))
        if r:
            lists.append([(n, label, i) for label, i in _identity_pictures(r)])
    out = []
    for k in range(max((len(x) for x in lists), default=0)):
        for lst in lists:
            if k < len(lst) and len(out) < limit:
                n, label, i = lst[k]
                row = db.one("SELECT path FROM images WHERE id=?", (i,))
                if row and row["path"] and Path(row["path"]).exists():
                    out.append((n, label, row["path"]))
    return out


def _look_pictures(pid: str, shot, limit: int = 3) -> list[tuple[str, str, str]]:
    """(name, description, image id) of the references a restyle shot should look like
    (2026-11-24: "can the change-a-video's-look option also take a reference image?")."""
    out = []
    for rid in _jl(shot["refs"], []):
        r = db.one("SELECT * FROM prod_refs WHERE id=? AND project_id=?", (rid, pid))
        if r and r["image_id"] and _img_done(r["image_id"]) and len(out) < limit:
            out.append((r["name"], (r["description"] or "").strip(), r["image_id"]))
    return out


def _canvas(params: dict) -> tuple[int, int]:
    """Viggle's own canvas (short edge 480), in the production's orientation."""
    w, h = int(params.get("width", 480)), int(params.get("height", 864))
    return (832, 480) if w >= h else (480, 832)


def _remake_source(path: Path, w: int, h: int, frames: int, out: Path) -> None:
    """The source on the canvas: 24 fps, cover-cropped, looped to cover `frames`, sound kept."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-i", str(path),
                    "-t", f"{frames / 24 + 0.3:.2f}", "-vf",
                    f"fps=24,scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
                    "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "160k", str(out)], check=True, timeout=600)


def _remake_frames(seconds) -> int:
    n = round((float(seconds or 5) * 24 - 5) / 17)
    return max(90, min(345, 17 * max(5, n) + 5))


def remake_prompt(pid: str, shot, frames: int) -> str:
    """MiniMax-H3's six-section prompt for a recast or restyle shot, written from the
    shot's options (Viggle's PROMPTING.md: describe the TARGET video, not an instruction;
    say what changes in retention_analysis)."""
    opts = _jl(shot["opts"], {})
    end = f"00:{frames / 24:06.3f}"
    what = (shot["prompt"] or "").strip()
    if shot["make"] == "recast":
        cast = []
        for i, c in enumerate(opts.get("cast") or []):
            r = db.one("SELECT name, description FROM prod_refs WHERE id=?", (c["ref"],))
            if r:
                cast.append((i + 1, r["name"], (r["description"] or "").strip(),
                             c.get("who") or "a person"))
        if not cast:      # no character list (an MCP scene): the person in the picture
            cast.append((1, "the new person", "", opts.get("who") or "the main person"))
        subjects = "\n".join(
            f"<Subject {n}> is {name} in <Picture 1>{': ' + desc if desc else ''}. "
            f"{name[:1].upper() + name[1:]} replaces {who} in <Video 1>." for n, name, desc, who in cast)
        names = ", ".join(f"{who} becomes <Subject {n}>, {name}" for n, name, _, who in cast)
        keep = "; ".join(f"<Subject {n}> (appears in [Shot 1]): fully_preserved - {name}'s face, "
                         f"hair and clothes stay identical in every frame, exactly as in <Picture 1>."
                         for n, name, _, _ in cast)
        none_of = " ".join(f"{who} never appears." for _, _, _, who in cast)
        extras = _recast_extras(pid, shot)
        pics = "".join(f"\n<Picture {k + 2}> is {label} (<Subject {n}>)."
                       for k, (n, label, _) in enumerate(extras))
        pics_keep = "".join(f"\n<Picture {k + 2}> (appears in [Shot 1]): reference - <Subject {n}>'s "
                            f"face and look exactly as in this {'close-up' if 'close-up' in label else 'photo'}."
                            for k, (n, label, _) in enumerate(extras))
        return f"""subject_definitions:
<Video 1> is the source video for the editing task.
<Picture 1> is a frame of the target video, from [Shot 1].{pics}
{subjects}

summary:
[video editing + character replacement] The target video is an edited version of <Video 1> in which {names}, each looking exactly as in <Picture 1>, doing exactly the same actions. Everything else is unchanged. The shot, the camera, the motion, the timing and the actions of <Video 1> are kept.

retention_analysis:
<Video 1> (source video editing): fully_preserved - the framing, the camera movement, the place, the light and the motion and timing of every action are copied frame for frame.
<Picture 1> (appears in [Shot 1]): reference - a frame of the target video; the replaced people's clothes and poses are taken from it.{pics_keep}
{keep}

detailed_description:
The target video is a realistic scene, exactly as <Video 1> but recast.
[Shot 1] From 00:00.000 to {end}, {what or 'everything happens exactly as in <Video 1>, with the same actions and timing'}. At 00:00.500 the scene matches <Picture 1>.
In every frame, from the first to the last, the recast people look exactly as in <Picture 1>: {none_of} Only these people change, and everything else stays as in <Video 1>.

overall_soundscape:
The soundtrack of <Video 1>, unchanged.

non_diegetic_music:
N/A"""
    change = (opts.get("change") or what or "the look changes").strip()
    looks = _look_pictures(pid, shot)
    look_defs = "".join(f"\n<Picture {k + 2}> shows {name}{': ' + desc if desc else ''} - "
                        f"the look to use." for k, (name, desc, _) in enumerate(looks))
    if looks:
        change += ", looking exactly like " + " and ".join(
            f"<Picture {k + 2}>" for k in range(len(looks)))
    return f"""subject_definitions:
<Video 1> is the source video for the target video edit.
<Picture 1> is a frame of the target video, exactly as the target video shows it at that moment.{look_defs}
<Subject 1> is what changes in <Video 1>: {change}

summary:
[video editing] The target video is an edited version of <Video 1>: {change}. The framing, the camera movement, the people's motion and the timing are exactly as in <Video 1>.

retention_analysis:
<Video 1> (source video editing): fully_preserved - the framing, the camera movement and the motion and timing of every action are copied frame for frame.
<Picture 1> (target keyframe): fully_preserved - the target video passes through this exact frame.
<Subject 1> (appears in [Shot 1]): changed - {change}, in every frame.

detailed_description:
The target video shows <Video 1> with this change: {change}.
[Shot 1] From 00:00.000 to {end}, everything moves exactly as in <Video 1>{', ' + what if what and what != change else ''}. At 00:00.500 the scene matches <Picture 1>.

overall_soundscape:
The soundtrack of <Video 1>, unchanged.

non_diegetic_music:
N/A"""


async def remake_picture_options(project, user: auth.User, shot, count: int) -> list[str]:
    """The painted frame: frame 12 of the source on the canvas, redrawn with the cast
    (recast) or the change (restyle). It becomes the shot's picture, and the remake
    pins it at that frame."""
    pid = project["id"]
    vids = _ref_video_items(pid, shot)
    if not vids:
        raise HTTPException(400, "add the video to remake under “The video to remake” first")
    w, h = _canvas(db.params_of(project))
    opts = _jl(shot["opts"], {})
    with tempfile.TemporaryDirectory() as tmp:
        src, frame = Path(tmp) / "src.mp4", Path(tmp) / "frame.png"
        _remake_source(vids[0][1], w, h, REMAKE_PIN + 30, src)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vf",
                        f"select=eq(n\\,{REMAKE_PIN})", "-frames:v", "1", str(frame)],
                       check=True, timeout=120)
        frame_id = _import_picture(user, "source-frame.png", frame.read_bytes())
    sources = [images.source_from_image(user, frame_id)]
    if shot["make"] == "recast":
        parts = []
        cast = [(c, db.one("SELECT * FROM prod_refs WHERE id=?", (c["ref"],)))
                for c in (opts.get("cast") or [])[:3]]
        cast = [(c, r) for c, r in cast if r and _identity_pictures(r)]
        room = 3 // max(1, len(cast))           # 4 sources: the frame + these
        for c, r in cast:
            nums = []
            for _, i in _identity_pictures(r)[:room]:
                sources.append(images.source_from_image(user, i))
                nums.append(str(len(sources)))
            parts.append(f"{c.get('who') or 'a person'} is replaced by {r['name']}: exactly the "
                         f"same face (face shape, eyes, nose, smile, skin, glasses and hair) as "
                         f"in picture{'s' if len(nums) > 1 else ''} {' and '.join(nums)}")
        if not parts:
            raise HTTPException(400, "say who each character replaces (and give them a picture)")
        text = ("The same photo as picture 1: same place, same poses and body positions, same "
                "camera angle and framing, but " + "; ".join(parts) + ". Photorealistic.")
    else:
        looks = _look_pictures(pid, shot)
        if not (opts.get("change") or "").strip() and not looks:
            raise HTTPException(400, "say what changes in the video, or add a reference "
                                     "picture of the look")
        for _, _, i in looks:
            sources.append(images.source_from_image(user, i))
        like = "; ".join(f"{name} looks exactly as in picture {k + 2}"
                         for k, (name, _, _) in enumerate(looks))
        text = (f"The same photo as picture 1: same framing, same people in the same poses, "
                f"but {opts.get('change') or 'restyled'}" + (f"; {like}" if like else "") + ".")
    ids = []
    for _ in range(max(1, min(4, count))):
        ids.append(await images.create(user, text[:1900], sources, orientation="landscape"
                                       if w > h else "portrait"))
    return ids


# ------------------------------------------------------------ reference videos
# A "Picture + references" shot can also follow up to 3 reference videos (another
# shot's clip, or an uploaded video): MiniMax H3 ref2v, `<Video k>` in the prompt.
# Measured 2026-11-24: a reference video steers the result much harder than the
# pictures -- the test clip followed its reference's motion and camera almost
# frame for frame.
MAX_REF_VIDEOS = 3
REF_VIDEO_MAX_S = 15.0
REF_VIDEO_UPLOAD_MB = 95          # Cloudflare refuses request bodies over 100 MB


def _ref_video_items(pid: str, shot) -> list[tuple[str, Path]]:
    """(label, file) for each reference video of a shot that still exists."""
    out = []
    for it in _jl(shot["ref_videos"] if "ref_videos" in shot.keys() else "[]", []):
        if it.get("shot"):
            row = db.one("SELECT title, idx, clip_path FROM shots WHERE id=? AND project_id=?",
                         (it["shot"], pid))
            if row and row["clip_path"] and Path(row["clip_path"]).exists():
                out.append((f"the clip of shot “{row['title'] or 'shot ' + str(row['idx'] + 1)}”",
                            Path(row["clip_path"])))
        elif it.get("path") and Path(it["path"]).exists():
            out.append((f"the video “{Path(it.get('name') or 'uploaded').stem}”", Path(it["path"])))
    return out


def _probe_media_seconds(path: Path) -> float:
    res = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, timeout=60)
    try:
        return float(res.stdout.strip().split("\n")[0])
    except ValueError:
        return 0.0


def _ref_video_b64(path: Path, width: int, height: int, seconds: float = 5.0) -> str:
    """As the model reads them: 24 fps, fitted inside the clip's size, no sound, and as
    long as the shot (2-15 s). A shorter reference is LOOPED to the shot's length: on
    2026-11-24 a 3.5 s dance reference for a 5 s shot ran out of motion and the model cut
    to one of the reference pictures for the rest of the clip."""
    want = max(2.1, min(REF_VIDEO_MAX_S, float(seconds or 5)))
    vf = (f"fps=24,scale={width}:{height}:force_original_aspect_ratio=decrease,"
          "scale=trunc(iw/2)*2:trunc(ih/2)*2")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "ref.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-i",
                        str(path), "-t", f"{want:.2f}", "-vf", vf, "-an", "-c:v", "libx264",
                        "-crf", "23", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(out)],
                       check=True, timeout=300)
        return wis.b64(out.read_bytes())


def add_ref_video(project, sid: str, from_shot: str | None = None,
                  upload: tuple[str, bytes] | None = None) -> dict:
    pid = project["id"]
    shot = _shot(pid, sid)
    if shot["make"] == "lipsync":
        raise HTTPException(400, "a lip-sync shot is made from its voice; switch it to "
                                 "“Keep characters true to their references” or “Copy a dance or movement” to use reference videos")
    items = _jl(shot["ref_videos"], [])
    if len(items) >= MAX_REF_VIDEOS:
        raise HTTPException(409, f"a shot takes at most {MAX_REF_VIDEOS} reference videos")
    if shot["make"] in ("motion", "recast", "restyle") and items:
        raise HTTPException(409, "a “Copy a dance or movement” shot copies one video; remove "
                                 "the current one first")
    if from_shot:
        src = db.one("SELECT id, clip_path FROM shots WHERE id=? AND project_id=?", (from_shot, pid))
        if src is None or src["id"] == sid:
            raise HTTPException(400, "pick another shot of this production")
        if not (src["clip_path"] and Path(src["clip_path"]).exists()):
            raise HTTPException(400, "that shot has no clip yet")
        if any(it.get("shot") == from_shot for it in items):
            raise HTTPException(409, "that clip is already a reference of this shot")
        item = {"shot": from_shot}
    elif upload:
        name, raw = upload
        if len(raw) > REF_VIDEO_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(413, f"videos up to {REF_VIDEO_UPLOAD_MB} MB")
        ext = (Path(name).suffix.lower() or ".mp4")[:6]
        dest = cfg.uploads_dir / pid / "refvideos"
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / f"{db.new_id('rv')}{ext}"
        path.write_bytes(raw)
        if _probe_media_seconds(path) <= 0.2:
            path.unlink(missing_ok=True)
            raise HTTPException(400, "that file is not a video this studio can read")
        item = {"path": str(path), "name": Path(name).name[:80]}
    else:
        raise HTTPException(400, "choose a shot's clip or upload a video")
    items.append(item)
    sets = {"ref_videos": json.dumps(items)}
    if shot["make"] not in ("perform", "motion", "recast", "restyle"):
        sets["make"] = "perform"           # only these models read videos
    if shot["status"] == "done":
        sets["status"] = "stale"
    db.run(f"UPDATE shots SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
           (*sets.values(), sid))
    _touch(pid)
    return {"ok": True, "count": len(items), "switched": "make" in sets}


def remove_ref_video(project, sid: str, index: int) -> None:
    pid = project["id"]
    shot = _shot(pid, sid)
    items = _jl(shot["ref_videos"], [])
    if not 0 <= index < len(items):
        raise HTTPException(404, "no such reference video")
    gone = items.pop(index)
    if gone.get("path"):
        Path(gone["path"]).unlink(missing_ok=True)
    db.run("UPDATE shots SET ref_videos=?, status=CASE WHEN status='done' THEN 'stale' ELSE "
           "status END WHERE id=?", (json.dumps(items), sid))
    _touch(pid)


def perform_payload(project_id: str, shot, image_path: str | None, params: dict) -> dict:
    """The WIS request for a performance or lip-sync shot (called by app/render.py)."""
    pid = project_id
    if shot.get("make") in TALK_MAKES:
        voices = _speaker_audio(pid, db.one("SELECT * FROM shots WHERE id=?", (shot["id"],)))
        need = 2 if shot["make"] == "talk2" else 1
        if not image_path or len(voices) < need:
            raise RuntimeError(f"{shot.get('title') or 'this shot'} needs a picture and "
                               f"{'both voices' if need == 2 else 'its voice or song'}")
        opts = _jl(shot.get("opts"), {})
        ctx = _shot_context(pid, shot)
        default = ("Two people in the picture talk to each other, natural expressions."
                   if need == 2 else "The person in the picture talks or sings, natural expression.")
        from app import promptsmith
        return {"model": cfg.TALK_MODEL, "client_request_id": shot["id"],
                "prompt": (promptsmith.current(pid, db.one("SELECT * FROM shots WHERE id=?",
                                                           (shot["id"],))).get("prompt")
                           or " ".join(x for x in (shot.get("prompt") or default, ctx) if x))[:3900],
                "width": int(params.get("width", 480)), "height": int(params.get("height", 864)),
                "image": {"b64": wis.b64(Path(image_path).read_bytes()), "mime": "image/png"},
                "reference_audio": [{"b64": wis.b64(p.read_bytes()), "mime": _audio_mime(p)}
                                    for p in voices[:need]],
                **({"speaker_layout": opts.get("layout", "left_right")} if need == 2 else {})}
    if shot.get("make") in REMAKE_MAKES:
        vids = _ref_video_items(pid, shot)
        if not image_path or not vids:
            raise RuntimeError(f"{shot.get('title') or 'this shot'} needs the video to remake and "
                               "its painted picture (🖼 Pictures)")
        w, h = _canvas(params)
        frames = _remake_frames(shot.get("seconds"))
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            _remake_source(vids[0][1], w, h, frames, src)
            video = wis.b64(src.read_bytes())
        row = db.one("SELECT * FROM shots WHERE id=?", (shot["id"],))
        if shot["make"] == "recast":
            extras = _recast_extras(pid, row)
            extra_paths = [p for _, _, p in extras]
        else:
            extras = _look_pictures(pid, row)
            extra_paths = [db.one("SELECT path FROM images WHERE id=?", (i,))["path"]
                           for _, _, i in extras]
        refs = [Path(image_path)] + [Path(p) for p in extra_paths if p and Path(p).exists()]
        out = {"model": cfg.RECAST_MODEL if shot["make"] == "recast" else cfg.RESTYLE_MODEL,
               "client_request_id": shot["id"], "width": w, "height": h, "length": frames,
               "prompt": (__import__("app.promptsmith", fromlist=["current"]).current(pid, row)
                          .get("prompt") or remake_prompt(pid, row, frames))[:3990],
               "reference_images": [{"b64": wis.b64(p.read_bytes()), "mime": "image/png"}
                                    for p in refs],
               "reference_videos": [{"b64": video, "mime": "video/mp4"}]}
        if not extras:
            out["pins"] = [REMAKE_PIN]  # Viggle: pin every picture or none
        return out
    if shot.get("make") == "motion":
        # Wan Animate 2: the shot picture (who, and where) performs the first reference
        # video's movements, frame for frame (2026-11-24: "girl dancing like the man in
        # the video" -- the reference model barely copied the dance).
        vids = _ref_video_items(pid, shot)
        image_path = image_path or _performer_picture(pid, db.one("SELECT * FROM shots WHERE id=?",
                                                                  (shot["id"],)))
        if not image_path or not vids:
            raise RuntimeError(f"{shot.get('title') or 'this shot'} copies a video's motion: it "
                               "needs the video, and a character (with a picture) in the shot "
                               "or a picture of its own")
        label, moves = vids[0]
        w, h = int(params.get("width", 480)), int(params.get("height", 864))
        secs = min(cfg.MOTION_MAX_SECONDS, int(shot.get("seconds") or 5))
        ctx = _shot_context(pid, shot)
        from app import promptsmith
        pol = promptsmith.current(pid, db.one("SELECT * FROM shots WHERE id=?", (shot["id"],)))
        return {"model": cfg.MOTION_MODEL, "client_request_id": shot["id"],
                "prompt": (pol.get("prompt") or " ".join(x for x in (shot.get("prompt") or "", ctx) if x)
                           or "The person in the picture performs the movements.")[:3900],
                "motion_prompt": pol.get("motion_prompt")
                or f"A reference video ({label}) of a person making these movements.",
                "seconds": secs, "width": w, "height": h,
                "image": {"b64": wis.b64(Path(image_path).read_bytes()), "mime": "image/png"},
                "reference_videos": [{"b64": _ref_video_b64(moves, w, h, secs),
                                      "mime": "video/mp4"}]}
    if shot.get("make") == "lipsync":
        lines = _dialogue_under(pid, shot["id"])
        if not image_path or not lines:
            raise RuntimeError(f"{shot.get('title') or 'a lip-sync shot'} needs a picture and a "
                               "use-in-video dialogue line under it")
        start, length = timings(pid).get(shot["id"], (0.0, float(shot.get("seconds") or 5)))
        ctx = _shot_context(pid, shot)
        said = " ".join(f"\"{c['text']}\"" for c in lines if c["text"])[:600]
        default = (f"{shot.get('title') or 'The character'}: speaks the line {said}, natural "
                   "expression, subtle head and body movement, steady camera.")
        from app import promptsmith
        return {"model": cfg.LIPSYNC_MODEL,
                "prompt": (promptsmith.current(pid, db.one("SELECT * FROM shots WHERE id=?",
                                                           (shot["id"],))).get("prompt")
                           or " ".join(x for x in (shot.get("prompt") or default, ctx) if x))[:3900],
                "seconds": min(cfg.LIPSYNC_MAX_SECONDS, int(shot.get("seconds") or 5)),
                "width": int(params.get("width", 480)), "height": int(params.get("height", 864)),
                "client_request_id": shot["id"],
                "image": {"b64": wis.b64(Path(image_path).read_bytes()), "mime": "image/png"},
                # The whole shot window, silence included, so the mouth is still
                # where the line has not started yet.
                "reference_audio": [{"b64": wis.b64(_window(lines, start, length)),
                                     "mime": "audio/wav"}]}
    # The shot's characters and places, then its scene's: main pictures first, views after,
    # up to the model's 9.
    refs = [db.one("SELECT * FROM prod_refs WHERE id=?", (r,)) for r in _shot_ref_ids(shot)]
    refs = [r for r in refs if r is not None]
    pics: list[tuple[str, str]] = []           # (label, path)
    if image_path:
        pics.append(("the opening frame", image_path))
    later = []
    for r in refs:
        if r["image_id"]:
            row = db.one("SELECT path FROM images WHERE id=?", (r["image_id"],))
            if row and row["path"] and Path(row["path"]).exists():
                pics.append((r["name"], row["path"]))
        for key, i in _angle_done(r).items():
            if key == "back":
                continue      # a plain back view got cut into a clip as its own shot (2026-11-24)
            row = db.one("SELECT path FROM images WHERE id=?", (i,))
            if row and row["path"] and Path(row["path"]).exists():
                later.append((f"{r['name']} ({key.replace('_', '-')})", row["path"]))
    pics = (pics + later)[:MAX_IMAGE_REFS]
    if _ref_video_items(pid, shot) and "ref_videos" in shot and pics:
        # With a reference video the model kept cutting to the extra reference pictures
        # (a back view, then a face close-up; 2026-11-24). The shot picture already shows
        # who is in it, so it goes alone.
        pics = pics[:1] if image_path else [p for p in pics if "(" not in p[0]]
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
    # The reference model can cut between shots; a storyboard shot is ONE shot, and the
    # pictures are looks, not shots (it inserted a plain reference photo as a cut).
    lines.append("One continuous shot with no cuts. The pictures only show what the people "
                 "and places look like; never show a picture on its own.")
    vids = _ref_video_items(pid, shot)[:MAX_REF_VIDEOS] if "ref_videos" in shot else []
    for k, (label, _) in enumerate(vids):
        lines.append(f"<Video {k + 1}> is {label}, a MOTION reference only: the people in this "
                     "shot make exactly the same body movements, move for move, with the same "
                     "rhythm and camera movement as in it, for the whole clip. Do not copy its "
                     "person, clothes or room.")
    ctx = _shot_context(pid, shot)
    from app import promptsmith
    pol = promptsmith.current(pid, db.one("SELECT * FROM shots WHERE id=?", (shot["id"],)))
    prompt = (" ".join([pol["prompt"], *lines]) if pol.get("prompt")
              else " ".join([shot.get("prompt") or "", *lines, ctx])).strip()
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
    if vids:
        payload["reference_videos"] = [
            {"b64": _ref_video_b64(p, payload["width"], payload["height"], payload["seconds"]),
             "mime": "video/mp4"}
            for _, p in vids]
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
    if not shots:
        raise HTTPException(400, "add shots first")
    # Mixing never waits for the video model: a shot without a clip is shown as its
    # still picture (or black), and an outdated clip is used as it is.
    assets = {a["id"]: a["path"] for a in db.query("SELECT id, path FROM assets WHERE project_id=?",
                                                   (pid,))}
    out = export_path(pid)
    work = out.parent / "mixwork"
    work.mkdir(parents=True, exist_ok=True)
    w, h = int(params.get("width", 480)), int(params.get("height", 864))
    k = 2 if params.get("upscale") in ("latent", "refine") else 1
    total = sum(float(s["seconds"] or 0) for s in shots)

    # 1) picture: every clip trimmed/padded to its shot length, scaled, joined.
    inputs, filters = [], []
    has_clip = []
    for i, s in enumerate(shots):
        clip = s["clip_path"] if s["clip_path"] and Path(s["clip_path"]).exists() else None
        pic = assets.get(s["asset_id"]) if s["asset_id"] else None
        has_clip.append(bool(clip))
        if clip:
            inputs += ["-i", clip]
        elif pic and Path(pic).exists():
            inputs += ["-loop", "1", "-t", str(s["seconds"]), "-i", pic]
        else:
            inputs += ["-f", "lavfi", "-t", str(s["seconds"]), "-i",
                       f"color=c=black:s={w * k}x{h * k}:r=24"]
        filters.append(f"[{i}:v]scale={w * k}:{h * k}:force_original_aspect_ratio=increase,"
                       f"crop={w * k}:{h * k},setsar=1,fps=24,"
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
        if has_clip[i] and (s["make"] in REMAKE_MAKES or
                            (s["make"] == "perform" and not mix["shot"]["mute"])) and \
                _has_audio(s["clip_path"]):
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
    _export_tasks.add(t := asyncio.create_task(go()))   # keep a reference: tasks can be collected
    t.add_done_callback(_export_tasks.discard)


# =========================================================================== proposals

def propose(project, title: str, summary: str, changes: list, shot_id: str | None = None,
            options: list | None = None) -> str:
    pid = project["id"]
    changes, _ = _validate_changes(changes) if changes else ([], [])
    prop_id = db.new_id("prp")
    now = db.now()
    db.run("INSERT INTO proposals (id, project_id, shot_id, title, summary, changes, options, "
           "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
           (prop_id, pid, shot_id, title[:120], summary[:2000], json.dumps(changes),
            json.dumps(options or []), "open", now, now))
    _touch(pid)
    return prop_id


OPS = {"set_story", "add_scene", "edit_scene", "delete_scene", "add_shot", "edit_shot",
       "delete_shot", "edit_clip", "delete_clip", "set_mix", "add_ref", "edit_ref", "delete_ref",
       "use_frame", "add_line"}

# The fields each edit op can change. An edit naming none of them changes nothing; it is
# reported as failed, never as applied (2026-11-25: "remove the old scenes" was proposed
# as three edit_scene items with only a scene_id, applied twice, and the scenes stayed).
EDIT_FIELDS = {
    "edit_scene": ("title", "summary", "position", "refs", "locked"),
    "edit_shot": ("title", "prompt", "seconds", "framing", "refs", "make", "position",
                  "start_from", "opts", "scene_id", "image_id"),
    "edit_ref": ("name", "description", "kind", "image_id", "speaker"),
    "edit_clip": ("start_s", "trim_s", "duration_s", "gain_db", "fade_in", "fade_out",
                  "use_in_video", "track", "name"),
}


def _guess_op(c: dict) -> str | None:
    """A change the model wrote without `op` (or under another name)."""
    for k in ("op", "type", "action", "kind", "operation"):
        if c.get(k) in OPS:
            return c[k]
    if c.get("shot_id"):
        return "edit_shot"
    if c.get("clip_id"):
        return "edit_clip"
    if c.get("ref_id"):
        return "edit_ref"
    if c.get("scene_id") and any(k in c for k in ("prompt", "framing", "seconds", "make")):
        return "add_shot"
    if c.get("scene_id"):
        return "edit_scene"
    if c.get("summary") is not None and c.get("title"):
        return "add_scene"
    if c.get("name") and c.get("kind") in REF_KINDS:
        return "add_ref"
    return None


def _validate_changes(changes, strict: bool = True) -> tuple[list, list]:
    """(usable changes, problems). One bad item no longer sinks a 47-change proposal
    (2026-11-24: a whole film's breakdown was refused for a few items without `op`)."""
    if not isinstance(changes, list):
        raise HTTPException(400, "changes must be a list of {op, ...}")
    good, bad = [], []
    for i, c in enumerate(changes):
        if not isinstance(c, dict):
            bad.append(f"change {i + 1} is not an object")
            continue
        op = _guess_op(c)
        if op is None:
            bad.append(f"change {i + 1} has no op (one of {', '.join(sorted(OPS))}): "
                       f"{json.dumps(c, ensure_ascii=False)[:120]}")
            continue
        good.append({**c, "op": op})
    if strict and bad and not good:
        raise HTTPException(400, "; ".join(bad[:5]))
    return good, bad


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


def _locked_target(pid: str, c: dict) -> str | None:
    """The locked scene a change would alter, if any (the person's scenes win)."""
    scene = None
    if c.get("op") in ("edit_scene", "delete_scene") or (c.get("op") == "add_shot"):
        scene = c.get("scene_id")
    elif c.get("op") in ("edit_shot", "delete_shot") or c.get("op") == "use_frame":
        sid = c.get("shot_id") or c.get("to_shot_id")
        row = db.one("SELECT scene_id FROM shots WHERE id=?", (sid,)) if sid else None
        scene = row["scene_id"] if row else None
        if c.get("op") == "edit_shot" and c.get("scene_id"):
            target = db.one("SELECT locked, title FROM prod_scenes WHERE id=?", (c["scene_id"],))
            if target and target["locked"]:
                return target["title"]
    if not scene:
        return None
    row = db.one("SELECT locked, title FROM prod_scenes WHERE id=? AND project_id=?", (scene, pid))
    return row["title"] if row and row["locked"] else None


def apply_changes(project, user: auth.User, changes: list, guard: bool = False) -> dict:
    """Run a list of edits. New scenes/shots can be named with `key` and referred
    to by that key later in the same list (e.g. add_scene key=s1, add_shot scene=s1)."""
    keys: dict[str, str] = {}
    pid = project["id"]

    def rid(v):
        return keys.get(v, v) if isinstance(v, str) else v

    def rref(v):
        """A reference by key, id or name."""
        if not isinstance(v, str):
            return v
        return keys.get(v) or resolve_ref(pid, v) or v

    def rrefs(vs):
        return [x for x in dict.fromkeys(rref(r) for r in vs or []) if x]

    done, failed = [], []
    for n, c in enumerate(changes):
      try:
        project = db.one("SELECT * FROM projects WHERE id=?", (pid,))
        c = _normalise(c)
        op = c.get("op")
        if guard:
            locked = _locked_target(pid, c)
            if locked:
                raise HTTPException(409, f"the scene “{locked}” is locked by you; left as it is")
        if op in EDIT_FIELDS and not any(k in c for k in EDIT_FIELDS[op]):
            what = op.split("_")[1]
            raise HTTPException(400, f"nothing to change in this {op} (give the fields to change; "
                                     f"to remove the {what}, use delete_{what})")
        if op == "add_scene" and c.get("fill_empty"):
            # The first scene of a breakdown fills the untouched "Scene 1" a new production
            # starts with -- decided now, at apply, not when the proposal was written: two
            # batches written against the same empty production both claimed it (2026-11-25).
            only = scenes_of(pid)
            if len(only) == 1 and not db.one("SELECT 1 FROM shots WHERE scene_id=?", (only[0]["id"],)) \
                    and not (only[0]["summary"] or "").strip():
                c = {**c, "op": "edit_scene", "scene_id": only[0]["id"]}
                op = "edit_scene"
                if c.get("key"):
                    keys[c["key"]] = only[0]["id"]
        if op == "set_story":
            set_story(project, title=c.get("title"), story=c.get("story"), aspect=c.get("aspect"))
        elif op == "add_scene":
            keys[c.get("key") or f"scene{len(keys)}"] = add_scene(
                project, c.get("title", ""), c.get("summary", ""), c.get("position"),
                rrefs(c.get("refs")))
        elif op == "edit_scene":
            edit_scene(project, rid(c.get("scene_id")), c.get("title"), c.get("summary"),
                       c.get("position"),
                       rrefs(c["refs"]) if isinstance(c.get("refs"), list) else None,
                       c.get("locked") if not guard else None)
        elif op == "delete_scene":
            delete_scene(project, rid(c.get("scene_id")))
        elif op == "add_shot":
            keys[c.get("key") or f"shot{len(keys)}"] = add_shot(
                project, rid(c.get("scene_id")), c.get("title", ""), c.get("prompt", ""),
                c.get("seconds"), c.get("framing", ""), rrefs(c.get("refs")),
                c.get("make") or ("perform" if db.params_of(project).get("video_refs")
                                  else "animate"), c.get("position"), c.get("image_id"), user,
                c.get("start_from", ""))
        elif op == "edit_shot":
            fields = {k: c.get(k) for k in ("title", "prompt", "seconds", "framing", "refs",
                                            "make", "position", "start_from", "opts") if k in c}
            if "scene_id" in c:
                fields["scene_id"] = rid(c["scene_id"])
            if fields.get("refs") is not None:
                fields["refs"] = rrefs(fields["refs"])
            edit_shot(project, rid(c.get("shot_id")), **fields)
            if c.get("image_id"):
                set_picture(project, user, rid(c["shot_id"]), c["image_id"])
        elif op == "delete_shot":
            delete_shot(project, rid(c.get("shot_id")))
        elif op == "add_line":
            add_script(project, rid(c.get("shot_id")), c.get("text", ""),
                       rref(c.get("ref_id")) if c.get("ref_id") else None,
                       c.get("instruct") or c.get("delivery") or "", c.get("offset_s"))
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
            key = c.get("key") or f"ref{len(keys)}"
            src = c.get("from_ref_id") or c.get("from_ref")
            have = ref_by_name(pid, c.get("name", "")) if c.get("name") else None
            if src:
                keys[key] = import_ref(project, user, str(src))
                # The story's own description wins over the copied one.
                fix = {k: c[k] for k in ("description", "name") if c.get(k)}
                if fix:
                    edit_ref(project, keys[key], user=user, **fix)
            elif have:
                # Already here (another batch added it, or the person did): use that one and
                # only fill what it lacks; the person's edits win.
                keys[key] = have["id"]
                fill = {k: c[k] for k in ("description", "speaker", "image_id")
                        if c.get(k) and not have[k]}
                if (c.get("voice") or c.get("voice_design")) and not have["voice_design"]:
                    fill["voice_design"] = c.get("voice") or c.get("voice_design")
                if fill:
                    edit_ref(project, have["id"], user=user, **fill)
                done.append("add_ref(existing)")
                continue
            else:
                keys[key] = add_ref(
                    project, c.get("kind", "character"), c.get("name", ""),
                    c.get("description", ""), c.get("image_id"), c.get("speaker"), user=user,
                    voice_design=str(c.get("voice") or c.get("voice_design") or ""))
        elif op == "edit_ref":
            target = rref(c.get("ref_id") or c.get("name"))
            edit_ref(project, target, user=user,
                     **{k: c.get(k) for k in ("name", "description", "kind", "image_id",
                                              "speaker", "voice_design") if k in c and not
                        (k == "name" and not c.get("ref_id"))})
        elif op == "delete_ref":
            delete_ref(project, rref(c.get("ref_id") or c.get("name")))
        done.append(op)
      except HTTPException as exc:
        failed.append(f"change {n + 1} ({c.get('op')}): {exc.detail}")
    out = {"applied": done, "new_ids": keys}
    if failed:
        out["failed"] = failed
    return out


def apply_proposal(project, user: auth.User, prop_id: str, option: str | None = None) -> dict:
    pid = project["id"]
    row = db.one("SELECT * FROM proposals WHERE id=? AND project_id=?", (prop_id, pid))
    if row is None:
        raise HTTPException(404, "no such proposal")
    if row["status"] != "open":
        raise HTTPException(409, f"this proposal was already {row['status']}")
    result = apply_changes(project, user, _jl(row["changes"], []), guard=True)
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


def _img_done(image_id: str) -> bool:
    row = db.one("SELECT status FROM images WHERE id=?", (image_id,))
    return bool(row and row["status"] == "succeeded")


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
                       "refs": _jl(sc["refs"], []), "locked": bool(sc["locked"]),
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
            "opts": _jl(s["opts"], {}),
            "needs_picture": needs_picture(pid, s),
            "ref_videos": [{"i": i, "label": lbl} for i, (lbl, _) in
                           enumerate(_ref_video_items(pid, s))],
            "auto": next((j["stage"] for j in params.get("_auto_shots") or [] if j["shot"] == s["id"]), None),
            "error": s["error"], "picture": _asset_url(pid, s["asset_id"]),
            "clip": f"/api/editor/{pid}/shots/{s['id']}/clip?v={int(Path(s['clip_path']).stat().st_mtime)}"
                    if clip_ok else None})
    clips = []
    for c in db.query("SELECT * FROM prod_clips WHERE project_id=? ORDER BY start_s", (pid,)):
        clips.append({k: c[k] for k in ("id", "track", "name", "source_kind", "source_id",
                                        "status", "error", "start_s", "trim_s", "duration_s",
                                        "source_s", "gain_db", "fade_in", "fade_out", "ref_id",
                                        "text", "instruct", "auto")} |
                     {"use_in_video": bool(c["use_in_video"]),
                      "url": f"/api/editor/{pid}/clips/{c['id']}/audio" if c["path"] else None})
    ref_views = []
    for r in refs:
        ref_views.append({"id": r["id"], "kind": r["kind"], "name": r["name"],
                          "description": r["description"], "speaker": r["speaker"],
                          "image_id": r["image_id"],
                          "image": base + service.stable_signed_path("image", r["image_id"], 86400)
                          if r["image_id"] and _img_done(r["image_id"]) else None,
                          "image_pending": bool(r["image_id"]) and not _img_done(r["image_id"]),
                          "mine": sorted(_marks(_jl(r["angles"], {}))[0]),
                          "skipped": sorted(_marks(_jl(r["angles"], {}))[1]),
                          "angles": [{"angle": k, "image_id": v,
                                      "status": (db.one("SELECT status FROM images WHERE id=?", (v,)) or
                                                 {"status": "gone"})["status"],
                                      "url": base + service.stable_signed_path("image", v, 86400)}
                                     for k, v in _jl(r["angles"], {}).items() if isinstance(v, str)],
                          "angles_waiting": "_auto" in _jl(r["angles"], {}),
                          "voice": f"/api/editor/{pid}/refs/{r['id']}/voice"
                          if r["voice_path"] else None,
                          "voice_design": r["voice_design"] or "",
                          "voice_designed": bool(r["voice_text"]) and bool(r["voice_path"]),
                          "voice_pending": bool(r["voice_job"]) or (
                              bool(r["voice_auto"]) and needs_voice(r))})
    props = []
    for p in db.query("SELECT * FROM proposals WHERE project_id=? AND status='open' "
                      "ORDER BY created_at", (pid,)):
        opts = []
        for o in _jl(p["options"], []):
            im = db.one("SELECT status, progress FROM images WHERE id=?", (o,))
            opts.append({"image_id": o, "status": im["status"] if im else "gone",
                         "progress": im["progress"] if im else 0,
                         "url": base + service.stable_signed_path("image", o, 86400)})
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
        "proposals": props, "export": export_view, "ai": ai_activity(project),
        "agent_applies": bool(params.get("agent_applies")),
        "planner": cfg.PLANNER_MODEL,
        "video_refs": bool(params.get("video_refs")),
        "thread_id": thread["id"] if thread else None,
        "speakers": sorted(speech.SPEAKERS), "framings": list(FRAMINGS),
        "limits": {m: cap_seconds(m) for m in MAKES},
        **service.retention(project["updated_at"]), "now": db.now(),
    }


STORY_FOR_AGENT = 60000     # characters; a 9:40 episode with its cast table is ~10,000


def _story_for_agent(story: str | None) -> str:
    """The WHOLE story for the planner (2026-11-25: it was cut at 2,500 characters, so the
    planner never saw most of the script or the cast table: it invented three scenes,
    "reconstructed" dialogue, drew Visnu golden-brown where the script said water-blue, and
    the person had to paste the script into the chat)."""
    text = (story or "").strip()
    if not text:
        return "Story: (none yet)"
    if len(text) <= STORY_FOR_AGENT:
        return f"Story (complete, {len(text)} characters):\n{text}\n(end of story)"
    return (f"Story (the first {STORY_FOR_AGENT} of {len(text)} characters; ask the person "
            f"for the rest if a scene you need is past this point):\n{text[:STORY_FOR_AGENT]}")


def describe_for_agent(project_id: str, focus_shot: str | None = None, compact: int = 0,
                       focus_scene: str | None = None) -> str:
    """The production as the planner reads it, compactly, with every id it needs.

    compact (agent._plan raises it only when the prompt would not fit the fast window):
      >=1  shots outside the focused scene lose their prompt text (ids, title, framing, use
           case, length, status, picture, cast and the use-case settings -- reference videos,
           voices, recast cast, restyle change -- stay, so the planner can act on any shot);
      >=2  also shortens reference descriptions."""
    project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    if project is None:
        return ""
    refresh_clips(project_id)
    params = db.params_of(project)
    tm = timings(project_id)
    if focus_shot and not focus_scene:
        fs = db.one("SELECT scene_id FROM shots WHERE id=?", (focus_shot,))
        focus_scene = fs["scene_id"] if fs else None
    lines = [f"PRODUCTION project_id={project_id} \"{project['title']}\" "
             f"(aspect {aspect_of(params)}"
             f"{', rendered 16:9 and cropped to scope: keep action in the middle band' if aspect_of(params) == '2.39:1' else ''}"
             f", render: {project['status']} "
             f"{project['progress']}% {project['message'] or ''})",
             _story_for_agent(project["story"])]
    refs = db.query("SELECT * FROM prod_refs WHERE project_id=? ORDER BY idx", (project_id,))
    if refs:
        lines.append("References:")
        for r in refs:
            bits = [f"picture image_id={r['image_id']}" if r["image_id"] else "no picture",
                    "voice sample" if r["voice_path"] else
                    (f"speaker {r['speaker']}" if r["speaker"] else "no voice")]
            lines.append(f"- ref_id={r['id']} {r['kind']} \"{r['name']}\": "
                         f"{r['description'][:260 if compact >= 2 else 700]} [{', '.join(bits)}]")
    shots = ordered_shots(project_id)
    names = {r["id"]: r["name"] for r in refs}
    for sc in scenes_of(project_id):
        cast = [names.get(r, r) for r in _jl(sc["refs"], [])]
        lines.append(f"SCENE scene_id={sc['id']} \"{sc['title']}\"{' LOCKED-BY-THE-PERSON' if sc['locked'] else ''} assets=[{', '.join(cast)}]: "
                     f"{sc['summary'][:300]}")
        in_scene = [x for x in shots if x["scene_id"] == sc["id"]]
        brief = compact >= 1 and sc["id"] != focus_scene
        if brief and in_scene:
            lines.append("  (shot prompts of this scene left out to save room; every id is here)")
        for s in in_scene:
            start, secs = tm[s["id"]]
            mark = " <== the person is looking at this shot" if s["id"] == focus_shot else ""
            lines.append(
                f"  shot_id={s['id']} #{s['idx'] + 1} \"{s['title']}\" {s['framing'] or ''} "
                f"{s['make']} {secs:g}s @{start:g}s status={s['status']}"
                f"{' starts-from-previous-last-frame' if s['start_from'] == 'prev_last' else (' picture' if s['asset_id'] else (' NO PICTURE' if needs_picture(project_id, s) else ' no-picture-needed'))} "
                f"refs=[{', '.join(names.get(r, r) for r in _jl(s['refs'], []))}] "
                # use-case settings (reference videos, voices, cast, change) ALWAYS stay:
                # they are short and the shot cannot be made without them
                f"{_agent_shot_extras(project_id, s)}"
                + ("" if brief else f"prompt: {(s['prompt'] or '')[:220]}") + mark)
    clips = db.query("SELECT * FROM prod_clips WHERE project_id=? ORDER BY track, start_s",
                     (project_id,))
    if clips:
        lines.append("Timeline audio:")
        for c in clips:
            lines.append(f"- clip_id={c['id']} {c['track']} @{c['start_s']:g}s "
                         f"{'SCRIPT(not voiced yet) ' if c['status'] == 'script' else ''}"
                         f"{(c['duration_s'] or 0):g}s gain {c['gain_db']:g}dB "
                         f"{'USE-IN-VIDEO ' if c['use_in_video'] else ''}{c['status']} "
                         f"\"{c['name'][:80]}\"")
    mix = _mix(project)
    lines.append("Mix: " + ", ".join(f"{t} {mix[t]['gain_db']:g}dB{' muted' if mix[t]['mute'] else ''}"
                                     for t in ("dialogue", "ambience", "music", "shot")) +
                 f", music/ambience duck {mix['duck_db']:g}dB under dialogue")
    try:
        lib = library_matches(project, refs)
    except Exception:                                              # noqa: BLE001
        lib = []
    if lib:
        lines.append("Characters/places of the SAME NAME in the person's OTHER productions. Reuse "
                     "one (add_ref{from_ref_id, description: the story's}) only if its look AGREES "
                     "with this story (age, skin, hair, costume); otherwise add a fresh reference:")
        for x in lib:
            lines.append(f"- from_ref_id={x['ref_id']} \"{x['name']}\" ({x['kind']}, from "
                         f"\"{x['production'][:40]}\"{', has picture' if x['has_picture'] else ''}): "
                         f"{x['description'][:220]}")
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
    changes, bad = _validate_changes(args.get("changes") or [])
    if db.params_of(project).get("agent_applies"):
        result = apply_changes(project, user, changes, guard=True)
        if bad:
            result.setdefault("failed", []).extend(bad)
        return {"applied": True, **result}
    pid = propose(project, str(args.get("title") or "Proposed changes"),
                  str(args.get("summary") or ""), changes, args.get("shot_id"))
    out = {"proposal_id": pid, "status": "waiting for the person to review and apply",
           "changes": len(changes)}
    if bad:
        out["skipped"] = bad[:10]
        out["next"] = "fix the skipped items in a second proposal"
    return out


def storyboard_changes(project_id: str, args: dict) -> list:
    """The nested shape a model writes for a whole story, as editor changes:
    {story?, refs:[{name,kind,description,speaker}], scenes:[{title,summary,
     shots:[{title,framing,seconds,prompt,make,refs:[names],start_from}]}]}."""
    changes: list = []
    if args.get("story") or args.get("title"):
        changes.append({"op": "set_story", **({"story": args["story"]} if args.get("story") else {}),
                        **({"title": args["title"]} if args.get("title") else {})})
    names: dict[str, str] = {name_key(r["name"]): r["id"] for r in
                             db.query("SELECT id, name FROM prod_refs WHERE project_id=?",
                                      (project_id,))}
    for i, r in enumerate(args.get("refs") or args.get("characters") or []):
        if not isinstance(r, dict) or not r.get("name"):
            continue
        if name_key(r["name"]) in names:
            continue
        key = f"ref{i}"
        names[name_key(r["name"])] = key
        kind = str(r.get("kind") or r.get("type") or "").lower()
        kind = {"place": "location", "setting": "location", "environment": "location",
                "set": "location", "background": "location", "object": "prop", "item": "prop",
                "person": "character", "animal": "character", "creature": "character"}.get(kind, kind)
        changes.append({"op": "add_ref", "key": key, "name": r["name"],
                        "kind": kind if kind in REF_KINDS else "character",
                        "description": r.get("description", ""),
                        **({"from_ref_id": r["from_ref_id"]} if r.get("from_ref_id") else {}),
                        **({"voice": r["voice"]} if r.get("voice") else {}),
                        **({"speaker": r["speaker"]} if r.get("speaker") in speech.SPEAKERS else {})})
    for si, sc in enumerate(args.get("scenes") or []):
        if not isinstance(sc, dict):
            continue
        cast = [names.get(name_key(n), n) for n in
                (sc.get("refs") or sc.get("assets") or sc.get("characters") or [])]
        key = f"scene{si}"
        # fill_empty: if, when this is APPLIED, the production still has only its untouched
        # "Scene 1", that scene is used; otherwise the scene is added after the others.
        changes.append({"op": "add_scene", "key": key, "title": sc.get("title", ""),
                        "summary": sc.get("summary", ""), "refs": cast,
                        **({"fill_empty": True} if si == 0 else {})})
        for hi, sh in enumerate(sc.get("shots") or []):
            if not isinstance(sh, dict):
                continue
            refs = [names.get(name_key(n), n) for n in sh.get("refs") or sh.get("characters") or []] \
                or cast
            lines = [d for d in (sh.get("dialogue") or sh.get("lines") or []) if isinstance(d, dict)
                     and (d.get("text") or d.get("line"))]
            shot = {"op": "add_shot", "key": f"s{si}_{hi}", "scene_id": key,
                    **{k: sh[k] for k in ("title", "framing", "seconds", "prompt", "make",
                                          "start_from") if sh.get(k) is not None},
                    "refs": [r for r in refs if r]}
            if sh.get("continues") is not None and hi > 0:
                shot["start_from"] = "prev_last" if sh.get("continues") else ""
            spoken = 0.0
            for d in lines:
                who = str(d.get("speaker") or d.get("character") or "")
                spoken += len(str(d.get("text") or d.get("line")).split()) / 2.6 + 0.7
            # A character speaking on camera: voice first, then the video from it.
            on_camera = any(names.get(name_key(d.get("speaker") or d.get("character") or ""))
                            in shot["refs"] for d in lines)
            if lines and on_camera:
                shot["make"] = "lipsync"     # voice first, video from the voice (the person's rule)
            if lines and float(shot.get("seconds") or 0) < spoken:
                shot["seconds"] = min(30 if shot.get("make") == "lipsync" else 11, round(spoken + 0.5))
            changes.append(shot)
            for d in lines:
                who = name_key(d.get("speaker") or d.get("character") or "")
                changes.append({"op": "add_line", "shot_id": f"s{si}_{hi}",
                                "ref_id": names.get(who), "text": d.get("text") or d.get("line"),
                                "instruct": d.get("delivery") or d.get("instruct") or ""})
    return changes


def _t_storyboard(user, args, base):
    project = _agent_project(user, args)
    changes = storyboard_changes(project["id"], args)
    if not changes:
        raise HTTPException(400, "give scenes (each with shots) and/or refs")
    n_sc = sum(1 for c in changes if c["op"] in ("add_scene", "edit_scene"))
    n_sh = sum(1 for c in changes if c["op"] == "add_shot")
    return _t_propose(user, {"project_id": project["id"],
                             "title": args.get("proposal_title") or
                             f"Storyboard: {n_sc} scene(s), {n_sh} shot(s)",
                             "summary": args.get("summary") or "", "changes": changes}, base)


def ref_prompt(project, r) -> str:
    """The default picture prompt for a reference: its look, in the film's style."""
    style = ""
    story = (project["story"] or "").lower()
    for word in ("3d animated", "3d animation", "pixar", "watercolor", "watercolour", "anime", "ghibli",
                 "2d", "cel shad", "claymation", "stop-motion", "photoreal", "cinematic"):
        if word in story:
            style = f" Style: {word}."
            break
    look = {"character": "Character reference sheet: full body, front view, neutral pose, plain soft "
                         "background, clear face, consistent design.",
            "location": "Establishing view of the place, no people, clear layout, natural light.",
            "prop": "The object alone, clear view, plain background.",
            "style": "A frame that shows this visual style.",
            "voice": "A portrait of the speaker, plain background."}.get(r["kind"], "")
    return f"{r['name']}, {r['description']}. {look}{style}".strip()


async def ref_picture_options(project, user: auth.User, ref_id: str, prompt: str = "",
                              count: int = 2, model: str | None = None) -> list[str]:
    """Picture options for a reference; the person picks one (nothing is set yet)."""
    r = _ref(project["id"], ref_id)
    params = db.params_of(project)
    text = (prompt.strip() or ref_prompt(project, r))[:1900]
    orient = "landscape" if r["kind"] == "location" and params.get("orientation") == "landscape" else \
        params.get("orientation", "portrait")
    return [await images.create(user, text, [], orientation=orient, model=model)
            for _ in range(max(1, min(4, int(count or 2))))]


async def make_ref_picture(project, user: auth.User, ref_id: str, prompt: str = "",
                           model: str | None = None) -> str:
    """A picture for a character/place reference (a clean, front-on look the shots copy)."""
    pid = project["id"]
    r = _ref(pid, ref_id)
    params = db.params_of(project)
    text = prompt.strip() or ref_prompt(project, r)
    src = []
    for i in _own_photos(r)[:4]:
        try:
            src.append(images.source_from_image(user, i))
        except HTTPException:
            pass
    if src:
        text += (f" Exactly the same {'place' if r['kind'] == 'location' else 'person'} as in "
                 f"the {'photo' if len(src) == 1 else str(len(src)) + ' photos'}: same face, "
                 "features, build and colours.")
    image_id = await images.create(user, text[:1900], src, orientation=params.get("orientation", "portrait"),
                                   model=model)
    db.run("UPDATE prod_refs SET image_id=? WHERE id=?", (image_id, ref_id))
    _want_angles(pid, ref_id)
    _touch(pid)
    return image_id


async def _t_ref_picture(user, args, base):
    project = _agent_project(user, args)
    # By id or by NAME: a reference added earlier in the same plan has no id the planner knew
    # (2026-11-25: "draw Teenage Bharata" was given newborn Bharata's id, so the newborn got a
    # teenager's picture and the person saw "two characters in one").
    want = str(args.get("ref") or args.get("name") or "").strip()
    ref_id = resolve_ref(project["id"], want) if want else None
    given = str(args.get("ref_id") or "")
    if given and want and ref_id and given != ref_id:
        given = ref_id                         # the name the planner meant wins over a stale id
    ref_id = ref_id or resolve_ref(project["id"], given)
    if not ref_id:
        raise HTTPException(404, f"no reference called {want or given!r} in this production yet "
                                 "(add it first, e.g. with propose_changes add_ref)")
    image_id = await make_ref_picture(project, user, ref_id,
                                      str(args.get("prompt") or ""), args.get("model"))
    row = await images.wait(image_id, 60)
    out = {"ref_id": ref_id, "name": _ref(project["id"], ref_id)["name"], "image_id": image_id,
           "status": row["status"]}
    if row["status"] == "succeeded":
        out["image_url"] = base + service.stable_signed_path("image", image_id, 86400)
    return out


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
        out["image_url"] = base + service.stable_signed_path("image", done[0], 86400)
        out["image_id"] = done[0]
    return out


async def _t_render(user, args, base):
    project = _agent_project(user, args)
    now, skipped = prepare_render(project, user, args.get("shot_ids") or None, "videos")
    if now:
        await render_shots(project, user, now)
    out = {"project_id": project["id"], "status": "queued" if now else "nothing to render",
           "rendering": len(now)}
    if skipped:
        out["need_pictures"] = skipped
        out["note"] = "these shots have no picture yet: draw_pictures first, and let the person look"
    return out


async def _t_refvid(user, args, base):
    project = _agent_project(user, args)
    out = add_ref_video(project, str(args.get("shot_id") or ""),
                        from_shot=str(args.get("from_shot_id") or ""))
    return {"project_id": project["id"], **out,
            "note": "the shot now follows that clip's motion and camera; render_shots remakes it "
                    "only when the person asks for videos"}


async def _t_draw(user, args, base):
    project = _agent_project(user, args)
    _, later = prepare_render(project, user, args.get("shot_ids") or None, "pictures")
    return {"project_id": project["id"], "drawing": len(later),
            "note": "characters' and places' pictures and views, then each shot's picture; "
                    "no videos are made (render_shots is a separate step the person asks for)"}


async def _t_dialogue(user, args, base):
    project = _agent_project(user, args)
    start = args.get("start_s")
    if start is None and args.get("shot_id"):
        start = timings(project["id"]).get(args["shot_id"], (0, 0))[0] + \
            float(args.get("offset_s") or 0.3)
    cid = await add_dialogue(project, user, args.get("ref_id"), str(args.get("text") or ""),
                             float(start or 0), str(args.get("instruct") or ""),
                             args.get("language"), True, args.get("speaker"))
    out = {"clip_id": cid, "status": "voicing (ready in about 15-60 s)"}
    shot_id = args.get("shot_id")
    if shot_id and args.get("video", True):
        shot = _shot(project["id"], shot_id)
        if shot["asset_id"] or shot["start_from"] == "prev_last":
            edit_shot(project, shot_id, make="lipsync")
            render_when_ready(project["id"], user, cid, shot_id)
            out["video"] = "the shot is generated lip-synced to this voice once the voice is ready"
        else:
            out["video"] = "the shot has no picture yet: give it one, then render it (lipsync)"
    return out


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


def _t_lines(user, args, base):
    """The script's lines onto existing shots, voiced automatically."""
    project = _agent_project(user, args)
    pid = project["id"]
    added, problems, lipsync = 0, [], set()
    for i, ln in enumerate(args.get("lines") or []):
        if not isinstance(ln, dict) or not (ln.get("text") or ln.get("line")) or not ln.get("shot_id"):
            problems.append(f"line {i + 1}: needs shot_id and text")
            continue
        who = str(ln.get("speaker") or ln.get("character") or "").strip()
        ref_id = resolve_ref(pid, who)
        try:
            shot = _shot(pid, str(ln["shot_id"]))
            add_script(db.one("SELECT * FROM projects WHERE id=?", (pid,)), shot["id"],
                       str(ln.get("text") or ln.get("line")), ref_id,
                       str(ln.get("delivery") or ln.get("instruct") or ""),
                       auto=args.get("voice", True) is not False)
            added += 1
            # A character in the shot speaking on camera: its video is made from the voice.
            if ref_id and ref_id in _jl(shot["refs"], []) and shot["make"] != "lipsync":
                lipsync.add(shot["id"])
        except HTTPException as exc:
            problems.append(f"line {i + 1}: {exc.detail}")
    for sid in lipsync:
        edit_shot(db.one("SELECT * FROM projects WHERE id=?", (pid,)), sid, make="lipsync")
    return {"added": added, "lipsync_shots": len(lipsync),
            "status": "voicing in each character's voice" if args.get("voice", True) is not False
            else "added as script lines", **({"skipped": problems[:10]} if problems else {})}


def _t_apply(user, args, base):
    project = _agent_project(user, args)
    want = args.get("proposal_ids") or []
    rows = db.query("SELECT id, title FROM proposals WHERE project_id=? AND status='open' "
                    "ORDER BY created_at", (project["id"],))
    rows = [r for r in rows if not want or r["id"] in want]
    if not rows:
        return {"applied": [], "note": "no open proposals"}
    done, problems = [], []
    for r in rows:
        try:
            res = apply_proposal(db.one("SELECT * FROM projects WHERE id=?", (project["id"],)),
                                 user, r["id"])
            done.append(r["title"])
            problems += res.get("failed") or []
        except HTTPException as exc:
            problems.append(f"{r['title']}: {exc.detail}")
    return {"applied": done, **({"skipped": problems[:10]} if problems else {})}


async def _t_search(user, args, base):
    from app import websearch
    try:
        return await websearch.search(str(args.get("query") or ""), args.get("count") or 6)
    except Exception as exc:                                   # noqa: BLE001
        raise HTTPException(502, f"search failed: {exc}")


async def _t_read_page(user, args, base):
    from app import websearch
    try:
        return await websearch.read_page(str(args.get("url") or ""), args.get("max_chars") or 6000)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:                                   # noqa: BLE001
        raise HTTPException(502, f"could not read that page: {exc}")


def _t_my_characters(user, args, base):
    project = _agent_project(user, args)
    found = library(user, project["id"], str(args.get("query") or ""), 30)
    return {"characters": found,
            "use": "add_ref {from_ref_id, name} (propose_changes or build_storyboard refs) copies "
                   "one into this production with its pictures, views and voice"}


async def _t_design_voice(user, args, base):
    project = _agent_project(user, args)
    rid = resolve_ref(project["id"], str(args.get("ref") or args.get("ref_id") or ""))
    if not rid:
        raise HTTPException(404, "no such character in this production")
    r = _ref(project["id"], rid)
    if args.get("voice") and r["voice_path"]:
        db.run("UPDATE prod_refs SET voice_path=NULL, voice_text=NULL, speaker=NULL WHERE id=?", (rid,))
    sid = await design_voice(project, user, rid, str(args.get("voice") or "") or None)
    return {"ref": r["name"], "voice": _ref(project["id"], rid)["voice_design"], "speech_id": sid,
            "status": "designing (a few seconds on the GPU); their lines follow it automatically"}


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
                        "duck_db}; add_ref{key,kind,name,description,image_id,speaker,from_ref_id "
                        "(copy one of the person's characters from another production)}; "
                        "edit_ref{ref_id,...}; delete_ref{ref_id}; add_line{shot_id,ref_id,text,"
                        "instruct}. REMOVING something is delete_scene/delete_shot/delete_ref/"
                        "delete_clip -- an edit_* with no fields to change does nothing and fails. "
                        "refs and ref_id may be given by the reference's NAME. A new "
                        "scene/shot/ref may be given a `key` and "
                        "used as its id later in the same list. framing: " +
                        "|".join(f for f in FRAMINGS if f) + ". make: animate (silent, picture "
                        "is the first frame) | perform (ref2v: picture + reference pictures + "
                        "use-in-video audio under the shot; makes its own sound) | lipsync "
                        "(Wan S2V: the picture speaks/sings the use-in-video dialogue line "
                        "under the shot, mouth in sync) | motion (Wan Animate 2: the person in the "
                        "picture makes exactly the movements of the shot's one reference video)."}}},
        "description": "Edit the production (story, scenes, shots, references, audio clips, "
                       "mix). The person reviews and applies the proposal unless they let you "
                       "apply directly. Use it for any change to the story structure or mix."},
    "build_storyboard": {
        "handler": _t_storyboard, "light": True,
        "schema": {"type": "object", "required": ["project_id", "scenes"], "properties": {
            **_PID,
            "story": {"type": "string", "description": "the full story text (optional)"},
            "refs": {"type": "array", "items": {"type": "object"},
                     "description": "characters/places: {name, kind, description, voice} -- "
                                    "voice (characters): how they sound, for designing their own "
                                    "voice: gender, age, pitch, timbre, pace, accent, emotion"},
            "scenes": {"type": "array", "items": {"type": "object"}, "description":
                       "in order: {title, summary, refs:[the characters/places in the scene, by "
                       "name], shots:[{title, framing, seconds, prompt, make, refs, "
                       "continues (true when it is one continuous take with the shot before), "
                       "dialogue:[{speaker: character name or 'Narrator', text, delivery}]}]} "
                       "(shots inherit the scene's refs; every line of the script goes in "
                       "`dialogue` of the shot where it is said, word for word)"},
            "proposal_title": {"type": "string"}, "summary": {"type": "string"}}},
        "description": "Lay out a story (or a part of it) as scenes and shots in one go, in a "
                       "natural nested shape; it becomes one proposal the person applies. Use "
                       "this for breakdowns; one call per 1-3 scenes keeps each answer short."},
    "make_ref_picture": {
        "handler": _t_ref_picture,
        "schema": {"type": "object", "required": ["project_id"], "properties": {
            **_PID, "ref": {"type": "string", "description": "the reference's NAME (preferred) "
                                                           "or ref_id"},
            "ref_id": {"type": "string"},
            "prompt": {"type": "string", "description": "default: from its name, kind, description"},
            "model": {"type": "string"}}},
        "description": "Make the picture for a character/place reference (its look for every "
                       "shot it is in). Do this for each scene's references before its shots."},
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
    "add_reference_video": {
        "handler": _t_refvid,
        "schema": {"type": "object", "required": ["project_id", "shot_id", "from_shot_id"],
                   "properties": {**_PID, "shot_id": {"type": "string"},
                                  "from_shot_id": {"type": "string", "description": "a shot whose "
                                                   "clip is already rendered"}}},
        "description": "Make a shot follow the motion and camera movement of another shot's clip "
                       "(a reference video, up to 3 per shot). Switches the shot to Picture + "
                       "references. The person can also upload their own reference videos."},
    "draw_pictures": {
        "handler": _t_draw,
        "schema": {"type": "object", "required": ["project_id"], "properties": {
            **_PID, "shot_ids": {"type": "array", "items": {"type": "string"},
                                 "description": "omit = every shot without a picture"}}},
        "description": "STEP 1: draw what the shots need -- missing character/place pictures "
                       "and views, then a picture for each shot without one. Never makes video."},
    "render_shots": {
        "handler": _t_render,
        "schema": {"type": "object", "required": ["project_id"], "properties": {
            **_PID, "shot_ids": {"type": "array", "items": {"type": "string"},
                                 "description": "omit = every shot without an up-to-date clip"}}},
        "description": "STEP 2: generate the video clips of these shots (only shots that have a "
                       "picture; the rest are reported). ~2 min per 5 s. Only when the person "
                       "asks for videos."},
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
            "video": {"type": "boolean", "description": "default true with shot_id: after the "
                                                        "voice is made, generate that shot's video "
                                                        "lip-synced to it"}}},
        "description": "Voice a line in a character's voice, FIRST, on the dialogue track; with "
                       "shot_id the shot's video is then generated lip-synced to that voice."},
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
    "add_lines": {
        "handler": _t_lines, "light": True,
        "schema": {"type": "object", "required": ["project_id", "lines"], "properties": {
            **_PID,
            "lines": {"type": "array", "items": {"type": "object"}, "description":
                      "in order: {shot_id, speaker: character name or 'Narrator', text (word for "
                      "word from the story), delivery}"},
            "voice": {"type": "boolean", "description": "default true: voice them now"}}},
        "description": "Put the script's dialogue and narration under the shots where they are "
                       "said (shots that already exist). They are voiced automatically in each "
                       "character's voice; shots where a character speaks become lipsync."},
    "apply_proposals": {
        "handler": _t_apply, "light": True,
        "schema": {"type": "object", "required": ["project_id"], "properties": {
            **_PID, "proposal_ids": {"type": "array", "items": {"type": "string"},
                                     "description": "omit = every open proposal"}}},
        "description": "Apply open proposals ONLY when the person has just said so in their own "
                       "words (\"apply\", \"approve\", \"go ahead\", \"yes, do it\"). Never on your "
                       "own initiative."},
    "design_voice": {
        "handler": _t_design_voice,
        "schema": {"type": "object", "required": ["project_id", "ref"], "properties": {
            **_PID, "ref": {"type": "string", "description": "the character's NAME (or ref_id)"},
            "voice": {"type": "string", "description": "voice description: gender, age, pitch, "
                      "timbre, pace, accent, emotion (omit = from the character description)"}}},
        "description": "Design (or redesign) a character's own voice from a description "
                       "(Qwen3-TTS VoiceDesign). Characters get one automatically when created; "
                       "use this when the person wants a different voice. Their lines are "
                       "voiced again in it."},
    "web_search": {
        "handler": _t_search, "light": True,
        "schema": {"type": "object", "required": ["query"], "properties": {
            "query": {"type": "string"}, "count": {"type": "integer", "description": "1-10"}}},
        "description": "Search the web (titles, links, snippets). Use it to check facts the "
                       "story relies on but does not spell out: how a deity, historical figure, "
                       "costume, weapon, place or period traditionally looks, a name's spelling, "
                       "a custom. Then read_web_page for detail."},
    "read_web_page": {
        "handler": _t_read_page, "light": True,
        "schema": {"type": "object", "required": ["url"], "properties": {
            "url": {"type": "string"}, "max_chars": {"type": "integer"}}},
        "description": "The text of one public web page (e.g. a web_search result)."},
    "my_characters": {
        "handler": _t_my_characters, "light": True,
        "schema": {"type": "object", "required": ["project_id"], "properties": {
            **_PID, "query": {"type": "string", "description": "a name or words of a description"}}},
        "description": "The person's characters, places and voices from their OTHER productions "
                       "(name, description, picture, which production). Reuse one with add_ref "
                       "{from_ref_id} to keep a character the same across stories."},
    "export_film": {
        "handler": _t_export,
        "schema": {"type": "object", "required": ["project_id"], "properties": {**_PID}},
        "description": "Join every shot's clip and mix the timeline into the final film."},
}
EDITOR_HEAVY = {"shot_picture_options", "render_shots", "add_dialogue", "add_music",
                "make_ref_picture", "draw_pictures", "design_voice"}


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


async def _refresh_refs(pid: str) -> None:
    for r in db.query("SELECT image_id, angles FROM prod_refs WHERE project_id=?", (pid,)):
        ids = [r["image_id"]] + [v for k, v in _jl(r["angles"], {}).items() if isinstance(v, str)]
        for i in [x for x in ids if x]:
            row = db.one("SELECT status FROM images WHERE id=?", (i,))
            if row and row["status"] not in images.TERMINAL:
                try:
                    await images.refresh(i)
                except Exception:                              # noqa: BLE001
                    pass


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
    await _refresh_refs(pid)
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
    if "video_refs" in b:
        # Every shot's "Video from" at once (lip-sync shots keep theirs), and the default
        # for new shots. A rendered clip made the other way is marked as changed.
        on = bool(b["video_refs"])
        params = db.params_of(db.one("SELECT * FROM projects WHERE id=?", (pid,)))
        params["video_refs"] = on
        db.set_params(pid, params)
        frm, to = ("animate", "perform") if on else ("perform", "animate")
        out["switched"] = db.one("SELECT COUNT(*) AS n FROM shots WHERE project_id=? AND make=?",
                                 (pid, frm))["n"]
        db.run("UPDATE shots SET make=?, status=CASE WHEN status='done' THEN 'stale' ELSE status "
               "END WHERE project_id=? AND make=?", (to, pid, frm))
        _touch(pid)
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
    changes, bad = _validate_changes(b.get("changes") or [])
    out = apply_changes(project, user, changes)
    if bad:
        out.setdefault("failed", []).extend(bad)
    return out


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


@router.post("/api/editor/{pid}/shots/{sid}/ref-videos")
async def api_add_ref_video(pid: str, sid: str, request: Request):
    user = _user(request)
    project = production(user, pid)
    if request.headers.get("content-type", "").startswith("multipart/"):
        form = await request.form()
        up = form.get("file")
        if up is None or not hasattr(up, "read"):
            raise HTTPException(400, "no file")
        return add_ref_video(project, sid, upload=(getattr(up, "filename", "video.mp4"),
                                                   await up.read()))
    return add_ref_video(project, sid, from_shot=str((await _body(request)).get("from_shot") or ""))


@router.post("/api/editor/{pid}/shots/{sid}/speaker-audio")
async def api_speaker_audio(pid: str, sid: str, request: Request):
    """multipart: file, index (0 = speaker 1 / the singer, 1 = speaker 2)."""
    user = _user(request)
    project = production(user, pid)
    form = await request.form()
    up = form.get("file")
    if up is None or not hasattr(up, "read"):
        raise HTTPException(400, "no file")
    return set_speaker_audio(project, sid, 1 if str(form.get("index")) == "1" else 0,
                             getattr(up, "filename", "voice.wav"), await up.read())


@router.post("/api/editor/{pid}/shots/{sid}/ref-videos/{index}/delete")
async def api_remove_ref_video(pid: str, sid: str, index: int, request: Request):
    user = _user(request)
    remove_ref_video(production(user, pid), sid, index)
    return {"ok": True}


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
    step = "pictures" if b.get("step") == "pictures" else "videos"
    now, other = prepare_render(project, user, b.get("shot_ids") or None, step)
    if now:
        await render_shots(project, user, now)
    if step == "pictures":
        return {"ok": True, "making_pictures": len(other)}
    return {"ok": True, "rendering": len(now), "need_pictures": len(other)}


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


@router.get("/api/editor/{pid}/library")
async def api_library(pid: str, request: Request, q: str = ""):
    """The person's characters/places/voices from their other productions."""
    user = _user(request)
    production(user, pid)
    base = service.base_url(request)
    out = library(user, pid, q, 120)
    for x in out:
        x["image"] = (base + service.stable_signed_path("image", x["image_id"], 86400)
                      if x["has_picture"] else None)
        x["here"] = bool(ref_by_name(pid, x["name"]))
    return {"items": out}


@router.post("/api/editor/{pid}/refs/import")
async def api_import_ref(pid: str, request: Request):
    user = _user(request)
    project = production(user, pid)
    b = await _body(request)
    ids = b.get("ref_ids") or ([b["ref_id"]] if b.get("ref_id") else [])
    if not ids:
        raise HTTPException(400, "pick at least one character")
    return {"ids": [import_ref(project, user, str(i)) for i in ids[:30]]}


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
                  getattr(voice, "filename", "") if vraw else "", user,
                  voice_design=str(form.get("voice_design") or ""))
    return {"id": rid}


@router.post("/api/editor/{pid}/refs/{rid}")
async def api_edit_ref(pid: str, rid: str, request: Request):
    user = _user(request)
    project = production(user, pid)
    form = await request.form()
    f = {k: form.get(k) for k in ("name", "description", "kind", "speaker", "image_id",
                                  "voice_design") if form.get(k) is not None}
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


@router.post("/api/editor/{pid}/refs/{rid}/generate")
async def api_ref_generate(pid: str, rid: str, request: Request):
    """{prompt?, count?} -> picture options for a reference (poll /images, then set one)."""
    user = _user(request)
    project = production(user, pid)
    b = await _body(request)
    ids = await ref_picture_options(project, user, rid, str(b.get("prompt") or ""),
                                    int(b.get("count") or 2), b.get("model"))
    return {"image_ids": ids, "prompt": str(b.get("prompt") or "") or ref_prompt(project, _ref(pid, rid))}


@router.post("/api/editor/{pid}/refs/{rid}/angles")
async def api_ref_angles(pid: str, rid: str, request: Request):
    """{only?: [angle]} -- (re)draw a character's turnaround from its main picture."""
    user = _user(request)
    project = production(user, pid)
    b = await _body(request)
    only = [a for a in (b.get("only") or []) if a in ANGLES or a in PLACE_VIEWS] or None
    model = str(b.get("model") or "") or None
    if model and model not in images.MODELS:
        raise HTTPException(400, f"model must be one of {', '.join(images.MODELS)}")
    return {"angles": await make_angles(project, user, rid, only, model)}


@router.post("/api/editor/{pid}/refs/{rid}/photos")
async def api_ref_photos(pid: str, rid: str, request: Request):
    """multipart: file (one or more), slot? ('main', a view key, or an existing photo_N)."""
    user = _user(request)
    project = production(user, pid)
    form = await request.form()
    files = [(getattr(f, "filename", "photo.png"), await f.read())
             for f in form.getlist("file") if hasattr(f, "read")]
    return add_ref_photos(project, user, rid, files, str(form.get("slot") or "") or None)


@router.post("/api/editor/{pid}/refs/{rid}/views/{key}/delete")
async def api_ref_view_delete(pid: str, rid: str, key: str, request: Request):
    user = _user(request)
    remove_ref_view(production(user, pid), rid, key)
    return {"ok": True}


@router.post("/api/editor/{pid}/ref-pictures")
async def api_refs_generate_missing(pid: str, request: Request):
    """A picture for every reference that has none (one each, set directly)."""
    user = _user(request)
    project = production(user, pid)
    made = []
    for r in db.query("SELECT id FROM prod_refs WHERE project_id=? AND (image_id IS NULL OR "
                      "image_id='') ORDER BY idx", (pid,)):
        made.append(await make_ref_picture(project, user, r["id"]))
    return {"made": len(made)}


@router.get("/api/editor/{pid}/images")
async def api_images(pid: str, request: Request, ids: str = ""):
    """Status and links of studio pictures (refreshing unfinished ones from WIS)."""
    user = _user(request)
    production(user, pid)
    out = []
    for i in [x for x in ids.split(",") if x][:8]:
        row = db.one("SELECT user_id, status FROM images WHERE id=?", (i,))
        if row is None or row["user_id"] != user.id:
            continue
        if row["status"] not in images.TERMINAL:
            try:
                row = await images.refresh(i)
            except Exception:                                  # noqa: BLE001
                pass
        out.append({"image_id": i, "status": row["status"],
                    "url": service.base_url(request) + service.stable_signed_path("image", i, 86400)
                    if row["status"] == "succeeded" else None})
    return {"images": out}


@router.post("/api/editor/{pid}/refs/{rid}/delete")
async def api_del_ref(pid: str, rid: str, request: Request):
    user = _user(request)
    delete_ref(production(user, pid), rid)
    return {"ok": True}


@router.post("/api/editor/{pid}/refs/{rid}/design-voice")
async def api_design_voice(pid: str, rid: str, request: Request):
    """Design (or redesign) this character's voice from {voice} or its description. The new
    voice replaces the old sample when it is ready, and their lines are voiced again in it."""
    user = _user(request)
    project = production(user, pid)
    b = await _body(request)
    r = _ref(pid, rid)
    brief = str(b.get("voice") or "").strip()
    if b.get("redo") or brief:
        # A new design: the old sample and preset go; lines follow the new voice.
        db.run("UPDATE prod_refs SET voice_path=NULL, voice_text=NULL, speaker=NULL WHERE id=?",
               (rid,))
        if r["voice_path"]:
            Path(r["voice_path"]).unlink(missing_ok=True)
    sid = await design_voice(project, user, rid, brief or None)
    return {"speech_id": sid, "voice": _ref(pid, rid)["voice_design"]}


@router.post("/api/editor/{pid}/design-voices")
async def api_design_voices(pid: str, request: Request):
    """Every character without a voice of their own gets one designed (and the lines they
    already have are voiced again in it)."""
    user = _user(request)
    production(user, pid)
    b = await _body(request)
    n = 0
    for r in db.query("SELECT * FROM prod_refs WHERE project_id=?", (pid,)):
        if b.get("replace_presets") and r["kind"] in VOICE_KINDS and r["speaker"] \
                and not r["voice_path"]:
            db.run("UPDATE prod_refs SET speaker=NULL WHERE id=?", (r["id"],))
            r = _ref(pid, r["id"])
        if needs_voice(r) and not r["voice_job"]:
            db.run("UPDATE prod_refs SET voice_auto=1 WHERE id=?", (r["id"],))
            n += 1
    _touch(pid)
    return {"characters": n}


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
        lip = b.get("lipsync_shot")
        if lip:
            shot = _shot(pid, str(lip))
            fields = {"make": "lipsync"}
            st, ln = timings(pid).get(shot["id"], (0, 0))
            if not shot["asset_id"] and shot["start_from"] != "prev_last":
                raise HTTPException(400, "the shot needs a picture to lip-sync")
            edit_shot(db.one("SELECT * FROM projects WHERE id=?", (pid,)), shot["id"], **fields)
            render_when_ready(pid, user, cid, shot["id"])
            return {"id": cid, "lipsync": shot["id"], "rendering": "when the line is ready"}
    elif kind in ("music", "ambience"):
        cid = await add_music(project, user, str(b.get("description") or ""), start,
                              b.get("duration"), kind, bool(b.get("instrumental", True)),
                              bool(b.get("use_in_video", False)), str(b.get("lyrics") or ""))
    else:
        cid = add_existing(project, user, str(b.get("track") or "music"),
                           speech_id=b.get("speech_id"), music_id=b.get("music_id"),
                           start_s=start, use_in_video=bool(b.get("use_in_video", False)))
    return {"id": cid}


@router.post("/api/editor/{pid}/clips/{cid}/voice")
async def api_voice_one(pid: str, cid: str, request: Request):
    user = _user(request)
    await voice_script(production(user, pid), user, cid)
    return {"ok": True}


@router.post("/api/editor/{pid}/voice-all")
async def api_voice_all(pid: str, request: Request):
    """Queue every unvoiced script line (voiced in order by the pending loop)."""
    user = _user(request)
    production(user, pid)
    n = db.one("SELECT COUNT(*) AS n FROM prod_clips WHERE project_id=? AND status='script'", (pid,))["n"]
    db.run("UPDATE prod_clips SET auto=1, error=NULL WHERE project_id=? AND status='script'", (pid,))
    return {"queued": n}


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


@router.post("/api/editor/{pid}/delete")
async def api_delete_production(pid: str, request: Request):
    """Delete the whole production: shots, clips, pictures in it, audio, scenes,
    references, proposals, its export and its conversation with the assistant."""
    from app import agent
    user = _user(request)
    project = service.owned_project(user, pid)
    # Stop its work first, as ■ Stop does: on 2026-11-24 a deleted production's place
    # views kept drawing in WIS (and held up the person's next production) because only
    # the rows were removed.
    if project["mode"] == "storyboard":
        try:
            await stop_all(project, user)
        except Exception as exc:                               # noqa: BLE001
            log.warning("stopping %s before deleting it: %s", pid, exc)
    if service.is_rendering(project):
        render.request_cancel(pid)
    for t in db.query("SELECT id FROM agent_threads WHERE project_id=?", (pid,)):
        agent.delete_thread(t["id"])
    service.delete_project_files(pid)          # files + the production's own rows
    db.run("DELETE FROM shots WHERE project_id=?", (pid,))
    db.run("DELETE FROM assets WHERE project_id=?", (pid,))
    db.run("DELETE FROM projects WHERE id=?", (pid,))
    log.info("production %s deleted by %s", pid, user.username)
    return {"ok": True}


def _pending_images(pid: str) -> list[str]:
    """Pictures this production is waiting for: shot pictures being made, picture
    options, references' main pictures and their views."""
    ids = set()
    project = db.one("SELECT params_json FROM projects WHERE id=?", (pid,))
    params = json.loads(project["params_json"] or "{}") if project else {}
    for j in params.get("_auto_shots") or []:
        if j.get("image"):
            ids.add(j["image"])
    for p in db.query("SELECT options FROM proposals WHERE project_id=? AND status='open'", (pid,)):
        ids.update(_jl(p["options"], []))
    for r in db.query("SELECT image_id, angles FROM prod_refs WHERE project_id=?", (pid,)):
        ids.update([r["image_id"]] + [v for v in _jl(r["angles"], {}).values() if isinstance(v, str)])
    out = []
    for i in ids:
        row = db.one("SELECT status FROM images WHERE id=?", (i,)) if i else None
        if row and row["status"] not in images.TERMINAL:
            out.append(i)
    return out


def ai_activity(project) -> dict:
    """What is running for this production right now (for the Stop button)."""
    pid = project["id"]
    params = db.params_of(project)
    thread = db.one("SELECT status FROM agent_threads WHERE project_id=? ORDER BY created_at LIMIT 1",
                    (pid,))
    voicing = db.one("SELECT COUNT(*) AS n FROM prod_clips WHERE project_id=? AND (status='pending' "
                     "OR (status='script' AND auto=1))", (pid,))["n"]
    return {"rendering": project["status"] in ("running", "queued"),
            "render_message": project["message"] if project["status"] in ("running", "queued") else "",
            "pictures": len(_pending_images(pid)),
            "shots_waiting": len(params.get("_auto_shots") or []) + len(params.get("_render_after") or []),
            "voicing": voicing,
            "agent": thread["status"] if thread and thread["status"] in
            ("queued", "planning", "executing", "watching") else None,
            "exporting": (params.get("_export") or {}).get("status") == "running"}


async def stop_all(project, user: auth.User) -> dict:
    """Stop every AI operation for this production. Finished work is kept."""
    from app import agent
    pid = project["id"]
    done = {}
    # the agent first, so it does not start anything new meanwhile
    for t in db.query("SELECT id, status FROM agent_threads WHERE project_id=? AND status IN "
                      "('queued','planning','executing','watching')", (pid,)):
        agent.stop(t["id"])
        done["agent"] = True
    params = db.params_of(project)
    done["shots_waiting"] = len(params.get("_auto_shots") or []) + len(params.get("_render_after") or [])
    params["_auto_shots"], params["_render_after"] = [], []
    db.set_params(pid, params)
    for r in db.query("SELECT id FROM prod_refs WHERE project_id=? AND angles LIKE '%_auto%'", (pid,)):
        db.run("UPDATE prod_refs SET angles='{}' WHERE id=?", (r["id"],))
    # pictures: cancel in WIS too, so they stop using the card
    pics = _pending_images(pid)
    for i in pics:
        row = db.one("SELECT wis_job_id FROM images WHERE id=?", (i,))
        if row and row["wis_job_id"]:
            try:
                await wis.cancel_job(row["wis_job_id"], route="images")
            except Exception:                                  # noqa: BLE001
                pass
        db.run("UPDATE images SET status='cancelled', error='stopped by you', updated_at=? WHERE id=?",
               (db.now(), i))
    done["pictures"] = len(pics)
    # voicing: queued lines go back to being script lines (voice them later)
    n_voice = 0
    for c in db.query("SELECT id, source_id FROM prod_clips WHERE project_id=? AND status='pending' "
                      "AND source_kind='speech'", (pid,)):
        row = db.one("SELECT status FROM speech WHERE id=?", (c["source_id"],))
        if row and row["status"] == "queued":
            db.run("UPDATE speech SET status='failed', error='stopped by you', updated_at=? WHERE id=?",
                   (db.now(), c["source_id"]))
            db.run("UPDATE prod_clips SET status='script', source_id=NULL, auto=0, duration_s=NULL "
                   "WHERE id=?", (c["id"],))
            n_voice += 1
    for c in db.query("SELECT id, source_id FROM prod_clips WHERE project_id=? AND status='pending' "
                      "AND source_kind='music'", (pid,)):
        row = db.one("SELECT status FROM music WHERE id=?", (c["source_id"],))
        if row and row["status"] == "queued":
            db.run("UPDATE music SET status='failed', error='stopped by you', updated_at=? WHERE id=?",
                   (db.now(), c["source_id"]))
            db.run("UPDATE prod_clips SET status='failed', error='stopped by you' WHERE id=?", (c["id"],))
            n_voice += 1
    n_voice += db.one("SELECT COUNT(*) AS n FROM prod_clips WHERE project_id=? AND status='script' "
                      "AND auto=1", (pid,))["n"]
    db.run("UPDATE prod_clips SET auto=0 WHERE project_id=? AND status='script'", (pid,))
    done["voicing"] = n_voice
    # the render, including its job in WIS
    project = db.one("SELECT * FROM projects WHERE id=?", (pid,))
    if service.is_rendering(project):
        render.request_cancel(pid)
        db.run("UPDATE shots SET status='pending' WHERE project_id=? AND status='running'", (pid,))
        done["render"] = True
    log.info("stop-all on %s by %s: %s", pid, user.username, done)
    _touch(pid)
    return done


_thread_locks: dict[str, asyncio.Lock] = {}


_freeing = {"task": None}


async def free_gpu() -> None:
    """Unload every model the studio and WIS hold (2026-11-24: an image model left loaded
    made a video run out of memory three times; the person asked for a force-clear).
    The studio's own engines first, then a moment's GPU turn in WIS's queue: when it
    starts, WIS's worker drops its models and ComfyUI frees the card. A job already
    generating finishes first (Stop ends it)."""
    from app import agent, gpuqueue, speech
    try:
        await speech.unload_kept()
        await gpuqueue._drop_lease()
        if not db.one("SELECT 1 FROM agent_threads WHERE status IN ('planning','executing')"):
            await agent._unload(cfg.PLANNER_MODEL)
        lease = await wis.lease_gpu("studio-free-the-gpu", 30)
        if lease:
            await wis.release_gpu(lease)
        log.info("GPU freed on request")
    except Exception as exc:                                   # noqa: BLE001
        log.warning("free the GPU: %s", exc)


@router.post("/api/editor/{pid}/free-gpu")
async def api_free_gpu(pid: str, request: Request):
    user = _user(request)
    production(user, pid)
    if _freeing["task"] is None or _freeing["task"].done():
        _freeing["task"] = asyncio.create_task(free_gpu())
    busy = db.one("SELECT COUNT(*) AS n FROM projects WHERE status='running'")["n"]
    return {"ok": True, "message": ("Freeing the GPU after the video in progress finishes "
                                    "(■ Stop ends it now)") if busy else "Freeing the GPU now"}


@router.post("/api/editor/{pid}/stop-all")
async def api_stop_all(pid: str, request: Request):
    user = _user(request)
    return await stop_all(production(user, pid), user)


@router.post("/api/editor/{pid}/agent")
async def api_agent_thread(pid: str, request: Request):
    """The production's conversation with the assistant (made on first use). Locked
    per production so simultaneous first requests can't make two conversations."""
    from app import agent
    user = _user(request)
    project = production(user, pid)
    async with _thread_locks.setdefault(pid, asyncio.Lock()):
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
