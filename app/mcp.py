"""The MCP endpoint -- Story Studio as a set of tools an assistant can call.

Streamable HTTP, stateless: every POST to /mcp is one JSON-RPC message and gets
one JSON answer. There is no session and no server-sent stream, because nothing
here pushes -- a render takes minutes, so the assistant starts it and polls
`get_status`, the same way the project page does.

The tools are a second front door onto `app.service`, not a second
implementation. Each handler finds the project, calls the same function the
form handler calls, and reports what the project looks like afterwards.

Vocabulary: the pages say "shot", assistants are told "scene". They are the
same row. A video made WITH scenes is a `storyboard` project; one made WITHOUT
is a `single` clip.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import ipaddress
import json
import logging
import socket
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app import auth, db, gpuqueue, images, music, oauth, render, service, speech, voices
from app.config import cfg

log = logging.getLogger("studio.mcp")
router = APIRouter()

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
VIDEO_LINK_TTL = 24 * 3600
UPLOAD_LINK_TTL = 3600

INSTRUCTIONS = """Story Studio turns pictures plus a story into a video on a single GPU.

CHARACTERS FIRST (storyboards): set_characters (each with the story's description, a voice \
brief, and goes_with = their outfit/object props and the film's style) -> draw_characters -> \
show the person the pictures and let them choose (choose_looks; their own picture also counts) \
-> set_scenes with each scene's `characters` (and `wear` where an outfit or object changes) -> \
draw_scene_pictures -> render_video. Nothing is made with a character before their look is \
chosen. edit_picture changes part of a picture and keeps the rest.

PODCASTS: for a podcast, debate or two-person discussion episode, call make_podcast with the \
topic (it researches the web, fact-checks, writes the script and builds an editor production \
with InfiniteTalk talking shots); poll get_podcast every minute or two and give the person \
editor_url when it is done.

THE EDITOR: every storyboard opens in the studio's editor at its `editor_url` (scenes, shots, \
characters, voices, music, the mix, the studio's own assistant). Give the person editor_url when \
they want to see or work on it there. A project with in_editor true is the person's production: \
make fixes IN it (set_scenes, set_characters, edit_picture, render_video with `scenes` for just \
the changed ones) -- never copy it into a new project, which leaves the person with several \
half-versions and none of them in their editor.

A project is made one of two ways, and `update_project` can switch it:
- storyboard = WITH scenes. One picture per scene, each animated for a few seconds, joined \
into one film. Silent unless a soundtrack was uploaded. Each scene has its own prompt, which \
should describe what MOVES and how the CAMERA moves -- the model already has the picture.
- single = WITHOUT scenes. One clip of up to ~15 s made from the story as the prompt, with up \
to 9 reference pictures (called <Picture 1>, <Picture 2>... in the prompt) and generated sound.

To regenerate a video: get_project, change the story or scenes, then render_video. In a \
storyboard, pass `scenes` to render_video to regenerate only those scenes and keep the rest. \
For a sharper result, update_project(upscale="refine" or "latent") delivers the video at twice \
the size (experimental, off by default, about 2.5x the render time). \
Rendering takes about 2 minutes per 5 seconds of video and runs one project at a time, so \
render_video returns at once: call get_status every 20-30 seconds until status is \
`succeeded`, then give the person the video_url. A picture that is in the conversation (generated \
or attached) goes in with add_file. A file on the person's own disk that you can reach with a \
shell goes in with get_upload_link; otherwise send them to the project page.

The studio also makes still pictures with generate_image: from a prompt alone (text-to-image), \
or from up to 4 source pictures plus a prompt saying what to change or combine (image-to-image). \
`model` chooses FLUX.2 klein (default) or Qwen-Image-2.1 (turbo or base; strong at lettering). \
Sources can be a file in this chat, a public URL, an earlier image_id, or a project's pictures. \
It usually finishes within the call (10-30 s); if it is still generating, call get_image. \
To edit a result, pass its image_id as a source. To use one in a video, add_file(image_id=...).

It also speaks text aloud with generate_speech (Qwen3-TTS): one of nine preset speakers, \
optionally steered with `instruct` ("calm and slow", "excited"), or a clone of a voice clip \
you supply. Sanskrit ślokas can be CHANTED (language=Sanskrit, Vāgdhenu; metre detected or given). \
Burmese (Myanmar) text is spoken by VoxCPM2 with its own four speakers (Thiri, \
Nandar, Kyaw, U_Ba) or a clone; Burmese script is detected automatically. Hindi has two \
options: four native Hindi voices on VoxCPM2 (Meera, the default, Anaya, Arjun, Vikram) or any of \
the nine Qwen voices (understandable, with a foreign accent); Devanagari is detected automatically. It usually finishes within the call; if not, call get_speech. Give the person the \
audio_url. To use a clip as a storyboard soundtrack, add_file(speech_id=...).

And it makes songs with generate_music (ACE-Step 1.5): describe a song in a sentence and it \
writes the lyrics too, or give your own lyrics ([Verse]/[Chorus] tags) and a style, optionally \
with a reference track for the sound. It can also cover, repaint a section of, extract a stem \
from, add a layer to, extend, or build a band around (accompany) an existing track. Speech and songs wait in a queue while the \
GPU is busy with pictures or videos; the reply gives the queue position. Poll get_music.

Personal voices: a person can train their OWN voice from their recordings (they create it and \
upload in their browser — get_voice_upload_link gives the page), then resing_song (change a \
song's singer to them, mixed back on the original music), sing_song (a new song in their range \
and manner, sung in their voice) and speak_as_voice. Voices are private to their owner. Poll \
get_voice_job for results.

Everything made or uploaded here is deleted 90 days after it last changed (each result says \
`deleted_on`). Tell the person to download anything they want to keep and save their own copy. \
Trained voices and voice profiles are the exception and are kept until deleted.

Each storyboard scene has a use case, and each use case is made by the model built for it \
(set_scenes `use_case`): animate (the default: the picture comes to life, MiniMax H3), perform \
(references guide it), longtalk (the person talks or sings up to 60 s, InfiniteTalk; give the \
voice with set_scene_voice), talk2 (two people in the picture talk, speaker 1 then 2; a voice \
each), motion (the person copies a video's movements exactly, Wan Animate 2; give the video \
with set_scene_video), recast (the person in the scene picture replaces someone in a video, \
keeping its motion, place and sound, Viggle Remake; opts.who) and restyle (change a video's \
look; opts.change). Talking, singing and remade scenes keep their sound in the film."""


# --------------------------------------------------------------------------- views

def _project(user: auth.User, args: dict):
    return service.owned_project(user, str(args.get("project_id") or ""))


def _video_url(project, base: str) -> str | None:
    path = project["output_path"]
    if project["status"] != "succeeded" or not path or not Path(path).exists():
        return None
    return base + service.signed_path("video", project["id"], VIDEO_LINK_TTL)


USE_CASES = {
    "animate": "Bring the picture to life: camera moves and gentle action from the scene's picture (MiniMax H3 image-to-video). Silent. The default.",
    "perform": "Keep characters true to references: the scene picture plus reference videos guide it (MiniMax H3 reference-to-video).",
    "longtalk": "Talk or sing for longer (up to 60 s): the person in the picture says or sings the scene's voice (InfiniteTalk). Needs set_scene_voice.",
    "talk2": "Two people talk: two people in the picture, speaker 1 then speaker 2, each with their own voice (InfiniteTalk). Needs set_scene_voice for both; opts.layout left_right (default) or right_left says who is speaker 1.",
    "motion": "Copy a dance or movement: the person in the picture makes exactly the movements of a video (Wan Animate 2). Needs set_scene_video. Silent.",
    "recast": "Recast a video: the person in the scene's picture replaces someone in a video, keeping its motion, camera, place and sound (Viggle Remake). The scene picture should be a frame of that video redrawn with the new person. Needs set_scene_video; opts.who names who is replaced. 4-14 s.",
    "restyle": "Change a video's look: outfits, weather, setting, style (Viggle Remake edit). The scene picture should be a frame of the video with the change. Needs set_scene_video; opts.change says what changes. 4-14 s.",
}

def _scenes(project_id: str, images: list) -> list[dict]:
    from app import editor
    number = {a["id"]: i + 1 for i, a in enumerate(images)}
    out = []
    for i, s in enumerate(service.shots_of(project_id)):
        d = {"scene": i + 1, "title": s["title"], "prompt": s["prompt"],
             "seconds": s["seconds"], "picture": number.get(s["asset_id"]),
             "use_case": s["make"] or "animate", "status": s["status"],
             **({"error": s["error"]} if s["error"] else {})}
        vids = editor._ref_video_items(project_id, s)
        if vids:
            d["video"] = vids[0][0]
        nm = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM prod_refs WHERE project_id=?", (project_id,))}
        own = [nm[x] for x in json.loads(s["refs"] or "[]") if x in nm]
        if own:
            d["characters"] = own
        w = json.loads(s["opts"] or "{}").get("wear") or {}
        if w:
            d["wear"] = {nm.get(k, k): [nm.get(x, x) for x in v] for k, v in w.items()}
        opts = json.loads(s["opts"] or "{}")
        if opts.get("speakers"):
            d["voices"] = len([x for x in opts["speakers"] if x.get("clip")])
        for k in ("layout", "who", "change"):
            if opts.get(k):
                d[k] = opts[k]
        out.append(d)
    return out


def _describe(project_id: str, base: str) -> dict:
    project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    assets = db.query("SELECT * FROM assets WHERE project_id=? ORDER BY kind, idx", (project_id,))
    images = [a for a in assets if a["kind"] == "image"]
    audios = [a for a in assets if a["kind"] == "audio"]
    params = db.params_of(project)
    single = project["mode"] == "single"
    max_img, max_aud = service.limits(project["mode"])
    settings = {"orientation": params.get("orientation", "portrait"),
                "size": f"{params.get('width', 480)}x{params.get('height', 864)}"}
    if single:
        settings["seconds"] = params.get("seconds", 10)
        settings["reference_fidelity"] = params.get("ref_image_size", "match")
    else:
        settings["seconds_per_new_scene"] = params.get("seconds", cfg.DEFAULT_SHOT_SECONDS)
        settings["match_cuts"] = bool(params.get("continuity"))
    settings["upscale"] = params.get("upscale", "off")
    out = {
        "project_id": project_id,
        "title": project["title"],
        **service.retention(project["updated_at"]),
        "mode": project["mode"],
        "made": ("without scenes: one clip from the story, pictures used as references, "
                 "sound generated" if single else
                 "with scenes: one picture per scene, clips joined into one film"),
        "story": project["story"],
        "settings": settings,
        "pictures": [{"picture": i + 1, "name": a["orig_name"],
                      **({"tag": f"<Picture {i + 1}>"} if single else {})}
                     for i, a in enumerate(images)],
        "picture_slots": max_img,
        "audio": [a["orig_name"] for a in audios],
        "audio_slots": max_aud,
        "status": project["status"],
        "progress": project["progress"],
        "message": project["message"],
        "page_url": f"{base}/projects/{project_id}",
    }
    if not single:
        # 2026-11-26: ChatGPT had no way to send the person to the editor and the project page
        # had no button, so "the projects are unable to move to the editor".
        out["editor_url"] = f"{base}/edit/{project_id}"
        out["in_editor"] = bool(params.get("editor"))
    if not single:
        out["scenes"] = _scenes(project_id, images)
        chars = db.query("SELECT name, kind, approved FROM prod_refs WHERE project_id=? ORDER BY idx", (project_id,))
        if chars:
            out["characters"] = [f"{c['name']} ({c['kind']}"
                                 f"{', look not chosen yet' if c['kind'] == 'character' and not c['approved'] else ''})"
                                 for c in chars]
    if project["error"]:
        out["error"] = project["error"]
    video = _video_url(project, base)
    if video:
        out["video_url"] = video
        out["video_has_sound"] = bool(project["has_audio"])
    return out


def _status(project_id: str, base: str) -> dict:
    project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    out = {"project_id": project_id, "status": project["status"],
           "progress": project["progress"], "message": project["message"],
           "renders_waiting": render.queue_depth()}
    if project["error"]:
        out["error"] = project["error"]
    if project["mode"] == "storyboard":
        from app import editor
        out["scenes"] = [{"scene": i + 1, "status": s["status"],
                          **({"has_picture": True} if s["asset_id"] else {}),
                          **({"error": s["error"]} if s["error"] else {})}
                         for i, s in enumerate(service.shots_of(project_id))]
        prog = editor.drawing_progress(project_id)
        if prog:
            out["pictures"] = {k: prog[k] for k in ("shots_done", "shots_total", "pictures_done",
                                                     "pictures_pending", "eta_s", "waiting_for_looks")}
    video = _video_url(project, base)
    if video:
        out["video_url"] = video
        out["video_has_sound"] = bool(project["has_audio"])
    elif service.is_rendering(project):
        out["next"] = "not finished; call get_status again in 20-30 seconds"
    return out


# --------------------------------------------------------------------------- fetching

async def _fetch_public(url: str, limit: int) -> tuple[str, str, bytes]:
    """Download a picture or audio file an assistant gave by URL.

    This is the one place the server makes a request to an address a caller
    chose, and it sits on a docker network next to the WIS API. So every hop is
    resolved first and refused unless all of its addresses are public, and
    redirects are followed by hand so each one gets the same check. The body is
    only ever parsed as media and never echoed back, which is what limits a
    resolver that changes its answer between the check and the connect.
    """
    loop = asyncio.get_running_loop()
    # Named honestly: several image hosts refuse a library's default agent.
    agent = {"User-Agent": "StoryStudio/1.0 (media fetch for a user-supplied URL)"}
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False,
                                 headers=agent) as client:
        for _ in range(4):
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise HTTPException(400, f"{url[:200]} is not an http(s) URL")
            try:
                infos = await loop.getaddrinfo(parts.hostname, parts.port or 443,
                                               type=socket.SOCK_STREAM)
            except OSError:
                raise HTTPException(400, f"could not resolve {parts.hostname}")
            if not infos or any(not ipaddress.ip_address(i[4][0]).is_global for i in infos):
                raise HTTPException(400, f"{parts.hostname} is not a public address")
            try:
                async with client.stream("GET", url) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        url = urljoin(url, resp.headers.get("location", ""))
                        continue
                    if resp.status_code >= 400:
                        raise HTTPException(400, f"{parts.hostname} answered HTTP "
                                                 f"{resp.status_code} for that URL")
                    chunks, size = [], 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise HTTPException(
                                413, f"that file is over {cfg.MAX_UPLOAD_MB} MB")
                        chunks.append(chunk)
                    name = Path(parts.path).name or "download"
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip()
                    return name, ctype, b"".join(chunks)
            except httpx.HTTPError as exc:
                raise HTTPException(400, f"could not download from {parts.hostname}: "
                                         f"{type(exc).__name__}")
    raise HTTPException(400, "too many redirects")


# --------------------------------------------------------------------------- tools

def _list_projects(user, args, base):
    rows = db.query("SELECT * FROM projects WHERE user_id=? ORDER BY updated_at DESC LIMIT 60",
                    (user.id,))
    out = []
    for p in rows:
        counts = db.one("SELECT COUNT(*) AS n FROM assets WHERE project_id=? AND kind='image'",
                        (p["id"],))
        out.append({"project_id": p["id"], "title": p["title"], "mode": p["mode"],
                    "status": p["status"], "pictures": counts["n"],
                    "has_video": _video_url(p, base) is not None,
                    **({"in_editor": bool(db.params_of(p).get("editor"))} if p["mode"] == "storyboard" else {}),
                    **service.retention(p["updated_at"])})
    return {"projects": out}


def _get_project(user, args, base):
    return _describe(_project(user, args)["id"], base)


def _create_project(user, args, base):
    title = str(args.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "a title is required")
    project_id = service.create_project(user, title, str(args.get("mode") or "storyboard"),
                                        str(args.get("story") or ""))
    out = _describe(project_id, base)
    out["next"] = ("add pictures with add_file (files in this chat), add_media (public URLs) "
                   "or get_upload_link (local files via curl), then write the scenes or the story and call render_video")
    return out


def _update_project(user, args, base):
    project = _project(user, args)
    orientation = args.get("orientation")
    if orientation is not None and orientation not in service.SIZES:
        raise HTTPException(400, "orientation must be portrait, landscape or square")
    fidelity = args.get("reference_fidelity")
    if fidelity is not None and fidelity not in ("match", "max"):
        raise HTTPException(400, "reference_fidelity must be match or max")
    service.save_settings(
        project,
        title=args.get("title"), story=args.get("story"), mode=args.get("mode"),
        orientation=orientation, seconds=args.get("seconds"),
        continuity=args.get("match_cuts"), ref_image_size=fidelity,
        upscale=args.get("upscale"))
    return _describe(project["id"], base)


async def _add_media(user, args, base):
    project = _project(user, args)
    urls = args.get("urls")
    if not isinstance(urls, list) or not urls or len(urls) > 12:
        raise HTTPException(400, "urls must be a list of 1-12 public http(s) links")
    limit = cfg.MAX_UPLOAD_MB * 1024 * 1024
    files = [await _fetch_public(str(u), limit) for u in urls]
    added = service.add_files(project, files)
    out = _describe(project["id"], base)
    out["added"] = added
    return out


async def _add_file(user, args, base):
    """One picture or audio file handed over by the assistant itself.

    ChatGPT keeps the images it generates (and the files a person attaches) in
    a sandbox that cannot reach this server, so an upload link is useless to
    it. What it CAN do is pass the file as a tool argument: for a field named
    in `openai/fileParams` it substitutes {download_url, file_id}, and the
    download happens from here. `base64` is the same door for clients that have
    the bytes but no such mechanism.
    """
    project = _project(user, args)
    if args.get("music_id"):
        added = music.add_to_project(user, str(args["music_id"]), project)
        out = _describe(project["id"], base)
        out["added"] = added
        return out
    if args.get("speech_id"):
        added = speech.add_to_project(user, str(args["speech_id"]), project)
        out = _describe(project["id"], base)
        out["added"] = added
        return out
    if args.get("image_id"):
        added = images.add_to_project(user, str(args["image_id"]), project)
        out = _describe(project["id"], base)
        out["added"] = added
        return out
    limit = cfg.MAX_UPLOAD_MB * 1024 * 1024
    ref, raw64 = args.get("file"), args.get("base64")
    given_name = str(args.get("filename") or "")
    if isinstance(ref, dict) and ref.get("download_url"):
        name, ctype, raw = await _fetch_public(str(ref["download_url"]), limit)
        name = given_name or str(ref.get("file_name") or ref.get("name") or name)
        ctype = str(ref.get("mime_type") or ctype)
    elif isinstance(ref, str) and ref.startswith(("http://", "https://")):
        name, ctype, raw = await _fetch_public(ref, limit)
        name = given_name or name
    elif isinstance(raw64, str) and raw64:
        if raw64.startswith("data:"):
            raw64 = raw64.partition(",")[2]
        try:
            raw = base64.b64decode(raw64, validate=False)
        except ValueError:
            raise HTTPException(400, "base64 is not valid base64")
        name, ctype = given_name or "upload.png", ""
    else:
        raise HTTPException(
            400, "pass `image_id` (a picture made with generate_image), `file` (an attached or "
                 "generated file) or `base64`. If your client can do none of these, use "
                 "add_media with a public URL, or get_upload_link.")
    added = service.add_files(project, [(name, ctype, raw)])
    out = _describe(project["id"], base)
    out["added"] = added
    return out


def _get_upload_link(user, args, base):
    project = _project(user, args)
    url = base + service.signed_path("upload", project["id"], UPLOAD_LINK_TTL)
    return {
        "upload_url": url,
        "expires_in_seconds": UPLOAD_LINK_TTL,
        "how": "POST multipart/form-data with one or more `files` fields. Pictures: PNG, JPEG "
               f"or WebP. Audio: WAV, MP3, FLAC, OGG or M4A. Up to {cfg.MAX_UPLOAD_MB} MB each. "
               "Pictures are numbered in the order they arrive.",
        "curl": f"curl -sS -F 'files=@first.png' -F 'files=@second.png' '{url}'",
        "or_in_a_browser": f"{base}/projects/{project['id']}",
    }


async def _plan_scenes(user, args, base):
    project = _project(user, args)
    how = await service.plan_shots(project)
    out = _describe(project["id"], base)
    out["planned_by"] = ("the studio's planner" if how == "planned" else
                         "an even split of the story, because the planner was busy with a "
                         "render — rewrite these with set_scenes, or call plan_scenes again "
                         "when nothing is rendering")
    return out


def _set_scenes(user, args, base):
    from app import editor
    project = _project(user, args)
    scenes = args.get("scenes")
    if not isinstance(scenes, list) or not scenes or not all(isinstance(s, dict) for s in scenes):
        raise HTTPException(400, "scenes must be a list of {scene, prompt, title?, seconds?}")
    service.set_scenes(project, [{k: v for k, v in s.items() if k != "seconds" or not
                                  s.get("use_case")} for s in scenes])
    # Use cases: one model per job (the same list as the editor's "What happens in this shot?").
    shots = service.shots_of(project["id"])
    for s in scenes:
        if s.get("characters") is not None or s.get("wear") is not None:
            shot = shots[int(s["scene"]) - 1]
            if s.get("characters") is not None:
                editor.edit_shot(project, shot["id"], refs=[
                    r for r in (editor.resolve_ref(project["id"], str(n)) for n in s["characters"]) if r])
            if isinstance(s.get("wear"), dict):
                editor.apply_changes(project, user, [{"op": "edit_shot", "shot_id": shot["id"],
                                                      "wear": s["wear"]}])
        if s.get("use_case") is None and s.get("opts") is None:
            continue
        if s.get("use_case") is not None and s["use_case"] not in USE_CASES:
            raise HTTPException(400, f"use_case must be one of {', '.join(USE_CASES)}")
        shot = shots[int(s["scene"]) - 1]
        fields = {}
        if s.get("use_case"):
            fields["make"] = s["use_case"]
        if s.get("seconds") is not None:
            fields["seconds"] = s["seconds"]
        if isinstance(s.get("opts"), dict):
            fields["opts"] = {k: v for k, v in s["opts"].items() if k in ("layout", "who", "change")}
        editor.edit_shot(project, shot["id"], **fields)
    return _describe(project["id"], base)


def _scene_shot(project, args):
    shots = service.shots_of(project["id"])
    try:
        n = int(args.get("scene"))
    except (TypeError, ValueError):
        raise HTTPException(400, "scene must be a scene number")
    if not 1 <= n <= len(shots):
        raise HTTPException(400, f"there is no scene {n}; this project has {len(shots)}")
    return shots[n - 1]


async def _set_scene_video(user, args, base):
    """The video a motion / recast / restyle / perform scene works from."""
    from app import editor
    project = _project(user, args)
    shot = _scene_shot(project, args)
    for i in reversed(range(len(json.loads(shot["ref_videos"] or "[]")))):
        if shot["make"] in ("motion", "recast", "restyle"):
            editor.remove_ref_video(project, shot["id"], i)       # these take one video
    if args.get("from_scene"):
        src = _scene_shot(project, {"scene": args["from_scene"]})
        out = editor.add_ref_video(project, shot["id"], from_shot=src["id"])
    elif args.get("video_url"):
        name, _ctype, raw = await _fetch_public(str(args["video_url"]),
                                                editor.REF_VIDEO_UPLOAD_MB * 1024 * 1024)
        out = editor.add_ref_video(project, shot["id"], upload=(name or "video.mp4", raw))
    else:
        raise HTTPException(400, "give from_scene (a rendered scene's clip) or video_url")
    return {**out, **_describe(project["id"], base)}


async def _set_scene_voice(user, args, base):
    """A voice or song for a longtalk / talk2 scene: a studio speech or music result, or a
    public audio URL. It is added as a dialogue clip and the scene uses it."""
    from app import editor
    project = _project(user, args)
    shot = _scene_shot(project, args)
    index = 1 if int(args.get("speaker") or 1) == 2 else 0
    if args.get("speech_id"):
        row = db.one("SELECT path, text FROM speech WHERE id=? AND user_id=? AND status='succeeded'",
                     (args["speech_id"], user.id))
        if row is None or not row["path"]:
            raise HTTPException(404, "no finished speech with that id")
        name, raw = (row["text"] or "voice")[:60] + Path(row["path"]).suffix, Path(row["path"]).read_bytes()
    elif args.get("music_id"):
        row = db.one("SELECT path FROM music WHERE id=? AND user_id=? AND status='succeeded'",
                     (args["music_id"], user.id))
        if row is None or not row["path"]:
            raise HTTPException(404, "no finished song with that id")
        name, raw = "song" + Path(row["path"]).suffix, Path(row["path"]).read_bytes()
    elif args.get("audio_url"):
        name, _ctype, raw = await _fetch_public(str(args["audio_url"]), 100 * 1024 * 1024)
    else:
        raise HTTPException(400, "give speech_id, music_id or audio_url")
    out = editor.set_speaker_audio(project, shot["id"], index, name or "voice.wav", raw)
    return {**out, **_describe(project["id"], base)}


# --------------------------------------------------------------------------- characters
# The same characters, props, styles, designed voices, looks and outfits as the studio's editor
# (2026-11-25, the person: "am assuming all these capabilities are available over MCP also?").

def _char_view(pid: str, r, base: str) -> dict:
    from app import editor
    names = {x["id"]: x["name"] for x in db.query("SELECT id, name FROM prod_refs WHERE project_id=?", (pid,))}
    out = {"name": r["name"], "kind": r["kind"], "description": r["description"] or ""}
    if r["image_id"]:
        row = db.one("SELECT status FROM images WHERE id=?", (r["image_id"],))
        out["picture"] = row["status"] if row else "gone"
        if row and row["status"] == "succeeded":
            out["picture_url"] = base + service.stable_signed_path("image", r["image_id"], 86400)
            out["image_id"] = r["image_id"]
    else:
        out["picture"] = "none yet"
    goes = [names.get(x, x) for x in editor.linked_ids(pid, r["id"])]
    if goes:
        out["goes_with"] = goes
    if r["kind"] in ("character", "voice"):
        out["voice"] = ("designed" if r["voice_text"] and r["voice_path"] else
                        "sample" if r["voice_path"] else f"preset {r['speaker']}" if r["speaker"] else
                        "being designed" if r["voice_job"] else "not yet (designed when chosen)")
        if r["voice_design"]:
            out["voice_brief"] = r["voice_design"]
    if r["kind"] == "character":
        out["look_chosen"] = bool(r["approved"]) or not editor.waits_for_looks(pid)
    return out


def _list_characters(user, args, base):
    from app import editor
    project = _project(user, args)
    rows = db.query("SELECT * FROM prod_refs WHERE project_id=? ORDER BY idx", (project["id"],))
    waiting = [r["name"] for r in rows if r["kind"] == "character" and not r["approved"]]
    out = {"project_id": project["id"], "characters": [_char_view(project["id"], r, base) for r in rows]}
    if waiting and editor.waits_for_looks(project["id"]):
        out["next"] = (f"show the person {', '.join(waiting)} (picture_url) and ask whether the looks "
                       "are right; when they say so call choose_looks. Until then no scene picture, "
                       "voice or video is made with them.")
    return out


async def _set_characters(user, args, base):
    """Add or change characters, places, props and styles by name."""
    from app import editor
    project = _project(user, args)
    pid = project["id"]
    items = args.get("characters") or []
    if not isinstance(items, list):
        raise HTTPException(400, "characters must be a list of {name, kind, description, ...}")
    changes = []
    for c in items:
        if not isinstance(c, dict) or not str(c.get("name") or "").strip():
            raise HTTPException(400, "every character needs a name")
        image_id = c.get("image_id")
        if c.get("picture_url"):
            name, _ctype, raw = await _fetch_public(str(c["picture_url"]), 30 * 1024 * 1024)
            image_id = editor._import_picture(user, name or "picture.png", raw)
        have = editor.ref_by_name(pid, c["name"])
        if have:
            op = {"op": "edit_ref", "ref_id": have["id"],
                  **{k: c[k] for k in ("description", "speaker", "kind") if c.get(k)},
                  **({"name": c["rename"]} if c.get("rename") else {}),
                  **({"voice_design": c["voice"]} if c.get("voice") else {})}
        else:
            op = {"op": "add_ref", "name": c["name"], "kind": c.get("kind") or "character",
                  **{k: c[k] for k in ("description", "speaker") if c.get(k)},
                  **({"voice": c["voice"]} if c.get("voice") else {})}
        if image_id:
            op["image_id"] = image_id
        if isinstance(c.get("goes_with"), list):
            op["with"] = c["goes_with"]
        changes.append(op)
    for n in args.get("remove") or []:
        rid = editor.resolve_ref(pid, str(n))
        if rid:
            changes.append({"op": "delete_ref", "ref_id": rid})
    res = editor.apply_changes(project, user, changes, guard=True)
    for c in items:                       # a picture the person gave chooses that look
        if c.get("image_id") or c.get("picture_url"):
            rid = editor.resolve_ref(pid, c.get("rename") or c["name"])
            if rid:
                editor.approve_refs(pid, [rid])
    out = _list_characters(user, {"project_id": pid}, base)
    if res.get("failed"):
        out["not_applied"] = res["failed"]
    return out


async def _draw_characters(user, args, base):
    """Pictures for characters/places/props/styles that have none (or `again`)."""
    from app import editor
    project = _project(user, args)
    pid = project["id"]
    want = [editor.resolve_ref(pid, str(n)) for n in args.get("names") or []] or \
        [r["id"] for r in db.query("SELECT id, image_id FROM prod_refs WHERE project_id=? "
                                   "AND kind IN ('character','location','prop','style')", (pid,))
         if not r["image_id"] or args.get("again")]
    want = [w for w in want if w]
    started = []
    for rid in want:
        await editor.make_ref_picture(project, user, rid,
                                      str(args.get("prompt") or "") if len(want) == 1 else "",
                                      args.get("model"))
        started.append(editor._ref(pid, rid)["name"])
    out = _list_characters(user, {"project_id": pid}, base)
    out["drawing"] = started
    out["next"] = ("pictures take 10-30 s each; call list_characters to see them (picture_url), "
                   "then show the person and ask them to choose") if started else "nothing to draw"
    return out


def _choose_looks(user, args, base):
    from app import editor
    project = _project(user, args)
    pid = project["id"]
    names = args.get("characters") or []
    ids = [editor.resolve_ref(pid, str(n)) for n in names] if names else \
        [r["id"] for r in db.query("SELECT id, kind, approved FROM prod_refs WHERE project_id=?", (pid,))
         if r["kind"] == "character" and not r["approved"]]
    n = editor.approve_refs(pid, [i for i in ids if i])
    out = _list_characters(user, {"project_id": pid}, base)
    out["chosen"] = n
    return out


async def _design_voice(user, args, base):
    from app import editor
    project = _project(user, args)
    out = await editor._t_design_voice(user, {"project_id": project["id"],
                                             "ref": args.get("character"), "voice": args.get("voice")}, base)
    return {**out, "next": "call list_characters in ~30 s; the character's lines are voiced again in it"}


async def _edit_picture(user, args, base):
    """Edited versions of a scene's or a character's picture; use_picture puts one in place."""
    from app import editor
    project = _project(user, args)
    ins = str(args.get("instruction") or "")
    if args.get("scene"):
        shot = _scene_shot(project, args)
        ids = await editor.edit_picture(user, editor._shot_picture_path(project["id"], shot), ins,
                                        int(args.get("count") or 2), args.get("model"))
    elif args.get("character"):
        rid = editor.resolve_ref(project["id"], str(args["character"]))
        if not rid:
            raise HTTPException(404, "no such character")
        ids = await editor.edit_ref_picture(project, user, rid, ins, int(args.get("count") or 2),
                                            args.get("model"))
    else:
        raise HTTPException(400, "give scene (a number) or character (a name)")
    rows = [await images.wait(i, IMAGE_CALL_BUDGET / max(1, len(ids))) for i in ids]
    return {"options": [_image_view(r, base, preview=True) for r in rows],
            "next": "show the person the options; use_picture puts the chosen image_id in place "
                    "(get_image if one is still generating)"}


def _use_picture(user, args, base):
    from app import editor
    project = _project(user, args)
    image_id = str(args.get("image_id") or "")
    if args.get("scene"):
        shot = _scene_shot(project, args)
        editor.set_picture(project, user, shot["id"], image_id)
    elif args.get("character"):
        rid = editor.resolve_ref(project["id"], str(args["character"]))
        if not rid:
            raise HTTPException(404, "no such character")
        editor.edit_ref(project, rid, user=user, image_id=image_id)
        editor.approve_refs(project["id"], [rid])      # the person picked it: the look is chosen
    else:
        raise HTTPException(400, "give scene (a number) or character (a name)")
    return _describe(project["id"], base)


def _draw_scene_pictures(user, args, base):
    """Each scene's picture, drawn from its characters and place (two options, the best kept)."""
    from app import editor
    project = _project(user, args)
    shots = service.shots_of(project["id"])
    only = [shots[int(n) - 1]["id"] for n in (args.get("scenes") or []) if 1 <= int(n) <= len(shots)]
    _, later = editor.prepare_render(project, user, only or None, "pictures")
    out = _status(project["id"], base)
    out["drawing_scenes"] = len(later)
    out["next"] = ("drawn in the background, one picture at a time (~25 s each): call get_status "
                   "for progress; scenes whose characters' looks are not chosen wait for "
                   "choose_looks")
    return out


async def _share_to_batiyao(user, args, base):
    from app import batiyao
    if not batiyao.enabled():
        raise HTTPException(503, "Batiyao sharing is not set up on this studio")
    st = batiyao.link_of(user.id)
    if not st or not st["access_token"] or \
            not {"write:media", "write:post"} <= set((st["scopes"] or "").split()):
        return {"connected": False,
                "next": f"the person connects Batiyao once in their browser: "
                        f"{base}/auth/batiyao/connect?next=/studio , then ask again"}
    kind = str(args.get("kind") or "")
    item = str(args.get("id") or "")
    if kind == "project":
        kind = "film" if db.params_of(_project(user, {"project_id": item})).get("editor") else "project"
    return await batiyao.share(user, kind, item, str(args.get("mode") or "post"),
                               str(args.get("text") or ""), str(args.get("rating") or "sfw"),
                               "", bool(args.get("confirm_spend")))


async def _render_video(user, args, base):
    project = _project(user, args)
    only = args.get("scenes")
    if only is not None:
        if not isinstance(only, list) or not all(isinstance(n, int) for n in only):
            raise HTTPException(400, "scenes must be a list of scene numbers, e.g. [2, 4]")
    depth = await service.start_render(project, user, only or None)
    log.info("user %s queued %s over mcp (depth %d)", user.username, project["id"], depth)
    out = _status(project["id"], base)
    out["next"] = ("queued; call get_status every 20-30 seconds until status is `succeeded`. "
                   "Roughly 2 minutes per 5 seconds of video being generated.")
    return out


def _get_status(user, args, base):
    return _status(_project(user, args)["id"], base)


# Kept well under the ~100 s at which the Cloudflare edge cuts a request: past
# that the assistant gets an error page instead of an image_id to poll with.
IMAGE_CALL_BUDGET = 70.0


def _image_view(row, base: str, preview: bool = False) -> dict:
    out = {"image_id": row["id"], "status": row["status"], "prompt": row["prompt"],
           "made_from": (f"{row['sources']} source picture(s)" if row["sources"]
                         else "the prompt alone"),
           "size": f"{row['width']}x{row['height']}",
           "model": row["model"] or cfg.IMAGE_MODEL,
           **service.retention(row["updated_at"])}
    if row["seed"] is not None:
        out["seed"] = row["seed"]
    if row["notes"]:
        out["notes"] = json.loads(row["notes"])
    if row["status"] == "succeeded" and row["path"] and Path(row["path"]).exists():
        out["image_url"] = base + service.signed_path("image", row["id"], VIDEO_LINK_TTL)
        out["link_expires_in_hours"] = VIDEO_LINK_TTL // 3600
        if preview:
            out["_preview"] = images.preview_jpeg(row["path"])
    elif row["status"] in ("failed", "cancelled"):
        out["error"] = row["error"] or row["status"]
    else:
        out["progress"] = row["progress"]
        out["next"] = "still generating; call get_image with this image_id in about 10 seconds"
    return out


async def _generate_image(user, args, base):
    started = asyncio.get_running_loop().time()
    sources: list[tuple[str, bytes]] = []
    limit = cfg.MAX_UPLOAD_MB * 1024 * 1024
    ref = args.get("file")
    if isinstance(ref, dict) and ref.get("download_url"):
        name, _, raw = await _fetch_public(str(ref["download_url"]), limit)
        sources.append((str(ref.get("file_name") or name), raw))
    raw64 = args.get("base64")
    if isinstance(raw64, str) and raw64:
        if raw64.startswith("data:"):
            raw64 = raw64.partition(",")[2]
        try:
            sources.append(("base64 source", base64.b64decode(raw64, validate=False)))
        except ValueError:
            raise HTTPException(400, "base64 is not valid base64")
    for image_id in args.get("image_ids") or []:
        sources.append(images.source_from_image(user, str(image_id)))
    pictures = args.get("pictures") or []
    if pictures and not args.get("project_id"):
        raise HTTPException(400, "`pictures` are numbered within a project; pass project_id too")
    for n in pictures:
        sources.append(images.source_from_picture(user, str(args["project_id"]), n))
    urls = args.get("urls") or []
    if len(sources) + len(urls) > images.MAX_SOURCES:
        raise HTTPException(400, f"at most {images.MAX_SOURCES} source pictures per image")
    for url in urls:
        name, _, raw = await _fetch_public(str(url), limit)
        sources.append((name, raw))

    image_id = await images.create(
        user, str(args.get("prompt") or ""), sources,
        orientation=args.get("orientation"), width=args.get("width"),
        height=args.get("height"), seed=args.get("seed"), model=args.get("model"))
    log.info("user %s started image %s (%d source(s))", user.username, image_id, len(sources))
    spent = asyncio.get_running_loop().time() - started
    row = await images.wait(image_id, max(5.0, IMAGE_CALL_BUDGET - spent))
    return _image_view(row, base, preview=True)


async def _get_image(user, args, base):
    row = images.owned_image(user, str(args.get("image_id") or ""))
    row = await images.refresh(row["id"])
    return _image_view(row, base, preview=True)


def _list_images(user, args, base):
    rows = db.query("SELECT * FROM images WHERE user_id=? ORDER BY created_at DESC LIMIT 30",
                    (user.id,))
    return {"images": [_image_view(r, base) for r in rows]}


def _queue_note(out: dict, kind: str, row, tool: str, key: str) -> None:
    """Where a queued speech/music job stands, so the assistant can tell the person."""
    pos = gpuqueue.position(kind, row["id"])
    if pos is not None:
        out["queue_position"] = pos
    if row["message"] and row["message"] not in ("queued", "generating"):
        out["waiting_for"] = row["message"]
    out["next"] = (f"{'queued' if row['status'] == 'queued' else 'generating'}; call {tool} "
                   f"with this {key} in about 20 seconds — it runs when the GPU is free")


SPEECH_CALL_BUDGET = 70.0


def _speech_view(row, base: str) -> dict:
    out = {"speech_id": row["id"], "status": row["status"], "text": row["text"],
           "voice": row["voice"] if row["voice"] != "clone" else "cloned from a reference clip",
           "language": row["language"], **service.retention(row["updated_at"])}
    if row["instruct"]:
        out["instruct"] = row["instruct"]
    p = speech.params(row)
    for k in ("match", "temperature", "seed"):
        if p.get(k) is not None:
            out[k] = p[k]
    if row["voice"] == "clone" and row["reference_text"]:
        out["reference_transcript_used"] = row["reference_text"]
    if row["status"] == "succeeded" and row["path"] and Path(row["path"]).exists():
        out["audio_url"] = base + service.signed_path("speech", row["id"], VIDEO_LINK_TTL)
        out["link_expires_in_hours"] = VIDEO_LINK_TTL // 3600
        if row["duration_s"]:
            out["duration_s"] = round(row["duration_s"], 1)
    elif row["status"] == "failed":
        out["error"] = row["error"] or "failed"
    else:
        _queue_note(out, "speech", row, "get_speech", "speech_id")
    return out


async def _generate_speech(user, args, base):
    started = asyncio.get_running_loop().time()
    limit = cfg.MAX_UPLOAD_MB * 1024 * 1024
    reference = None
    ref, raw64, url = args.get("file"), args.get("reference_base64"), args.get("reference_url")
    if sum(bool(x) for x in (ref, raw64, url)) > 1:
        raise HTTPException(400, "give one reference clip: `file`, `reference_base64` or "
                                 "`reference_url`")
    if isinstance(ref, dict) and ref.get("download_url"):
        _, _, reference = await _fetch_public(str(ref["download_url"]), limit)
    elif isinstance(raw64, str) and raw64:
        if raw64.startswith("data:"):
            raw64 = raw64.partition(",")[2]
        try:
            reference = base64.b64decode(raw64, validate=False)
        except ValueError:
            raise HTTPException(400, "reference_base64 is not valid base64")
    elif isinstance(url, str) and url:
        _, _, reference = await _fetch_public(url, limit)
    if args.get("reference_text") and reference is None:
        raise HTTPException(400, "reference_text goes with a reference clip; none was given")

    ids = await speech.create(
        user, str(args.get("text") or ""), speaker=args.get("speaker"),
        language=args.get("language"), instruct=args.get("instruct"),
        reference=reference, reference_text=args.get("reference_text"),
        match=args.get("match"), temperature=args.get("temperature"), seed=args.get("seed"),
        takes=args.get("takes") or 1, meter=args.get("meter"))
    log.info("user %s started speech %s (%s)", user.username, ",".join(ids),
             "clone" if reference is not None else args.get("speaker") or "default")
    spent = asyncio.get_running_loop().time() - started
    rows = []
    for speech_id in ids:
        left = SPEECH_CALL_BUDGET - (asyncio.get_running_loop().time() - started)
        rows.append(await speech.wait(speech_id, max(1.0, left)))
    if len(rows) == 1:
        return _speech_view(rows[0], base)
    return {"takes": [_speech_view(r, base) for r in rows],
            "note": "several takes of the same text with different seeds — listen and keep "
                    "the best; repeat one with its seed"}


def _get_speech(user, args, base):
    return _speech_view(speech.owned(user, str(args.get("speech_id") or "")), base)


def _list_speech(user, args, base):
    rows = db.query("SELECT * FROM speech WHERE user_id=? ORDER BY created_at DESC LIMIT 30",
                    (user.id,))
    return {"speech": [_speech_view(r, base) for r in rows]}


MUSIC_CALL_BUDGET = 70.0


def _music_view(row, base: str) -> dict:
    p = json.loads(row["params"])
    out = {"music_id": row["id"], "status": row["status"], "task": row["task"],
           "title": row["title"], **service.retention(row["updated_at"])}
    for k in ("description", "style", "language", "duration", "track"):
        if p.get(k):
            out[f"requested_{k}"] = p[k]
    if p.get("instrumental"):
        out["instrumental"] = True
    if row["status"] == "succeeded" and row["path"] and Path(row["path"]).exists():
        out["audio_url"] = base + service.signed_path("music", row["id"], VIDEO_LINK_TTL)
        out["link_expires_in_hours"] = VIDEO_LINK_TTL // 3600
        if row["duration_s"]:
            out["duration_s"] = row["duration_s"]
        m = music.meta(row)
        out.update({k: m[k] for k in ("lyrics", "style", "bpm", "key", "seed") if m.get(k)})
    elif row["status"] == "failed":
        out["error"] = row["error"] or "failed"
    else:
        _queue_note(out, "music", row, "get_music", "music_id")
    return out


async def _audio_arg(user, args, prefix: str, limit: int) -> bytes | None:
    """One audio input given as <prefix>_music_id / _speech_id / _file / _base64 / _url."""
    given = [k for k in ("music_id", "speech_id", "file", "base64", "url")
             if args.get(f"{prefix}_{k}")]
    if len(given) > 1:
        raise HTTPException(400, f"give one {prefix} track, not several")
    if not given:
        return None
    kind, value = given[0], args[f"{prefix}_{given[0]}"]
    if kind == "music_id":
        return music.source_from_music(user, str(value))
    if kind == "speech_id":
        row = speech.owned(user, str(value))
        if row["status"] != "succeeded" or not row["path"]:
            raise HTTPException(400, f"speech {row['id']} is not finished")
        return Path(row["path"]).read_bytes()
    if kind == "file":
        if not isinstance(value, dict) or not value.get("download_url"):
            raise HTTPException(400, f"{prefix}_file needs a download_url")
        return (await _fetch_public(str(value["download_url"]), limit))[2]
    if kind == "base64":
        raw64 = str(value)
        if raw64.startswith("data:"):
            raw64 = raw64.partition(",")[2]
        try:
            return base64.b64decode(raw64, validate=False)
        except ValueError:
            raise HTTPException(400, f"{prefix}_base64 is not valid base64")
    return (await _fetch_public(str(value), limit))[2]


async def _generate_music(user, args, base):
    started = asyncio.get_running_loop().time()
    limit = 50 * 1024 * 1024
    source = await _audio_arg(user, args, "source", limit)
    reference = await _audio_arg(user, args, "reference", limit)
    music_id = await music.create(
        user, task=str(args.get("task") or ("describe" if args.get("description") and not
                                             (args.get("lyrics") or args.get("style"))
                                             else "custom")),
        description=args.get("description") or "", style=args.get("style") or "",
        lyrics=args.get("lyrics") or "", instrumental=bool(args.get("instrumental")),
        language=args.get("language"), duration=args.get("duration"), bpm=args.get("bpm"),
        key=args.get("key") or "", time_signature=str(args.get("time_signature") or ""),
        seed=args.get("seed"), strength=args.get("strength"), start=args.get("start"),
        end=args.get("end"), track=args.get("track") or "",
        instruments=args.get("instruments") or [], add_seconds=args.get("add_seconds"),
        title=args.get("title") or "", source=source,
        source_music_id=args.get("source_music_id") or None, reference=reference)
    log.info("user %s queued music %s (%s)", user.username, music_id, args.get("task"))
    spent = asyncio.get_running_loop().time() - started
    row = await music.wait(music_id, max(5.0, MUSIC_CALL_BUDGET - spent))
    return _music_view(row, base)


def _get_music(user, args, base):
    return _music_view(music.owned(user, str(args.get("music_id") or "")), base)


def _list_music(user, args, base):
    rows = db.query("SELECT * FROM music WHERE user_id=? ORDER BY created_at DESC LIMIT 30",
                    (user.id,))
    return {"music": [_music_view(r, base) for r in rows]}


def _cancel_render(user, args, base):
    project = _project(user, args)
    if not service.is_rendering(project):
        raise HTTPException(409, "this project is not rendering")
    if render.request_cancel(project["id"]):
        db.touch_project(project["id"], message="cancelling…")
    return _status(project["id"], base)


_PID = {"project_id": {"type": "string", "description": "A project id, e.g. prj_1a2b…"}}
_MODE = {"type": "string", "enum": list(service.MODES),
         "description": "storyboard = WITH scenes (one picture per scene, joined). "
                        "single = WITHOUT scenes (one clip, pictures are references, has sound)."}



# --------------------------------------------------------------------------- voices
# Thin wrappers over app.voices (the same rules the /v1 API applies). Creating a voice
# and uploading recordings are deliberately NOT tools: the person attests and uploads
# in their own browser, never an assistant acting for them.

VOICE_CALL_BUDGET = 70.0


def _voice_brief(v: dict) -> dict:
    out = {"voice_id": v["id"], "name": v["name"], "status": v["status"],
           "recordings": v["dataset"]["clips_ok"],
           "minutes_singing": round(v["dataset"]["singing_s"] / 60, 1),
           "minutes_speech": round(v["dataset"]["speech_s"] / 60, 1),
           "kept": v["kept"]}
    if v["dataset"]["minutes_needed"]:
        out["minutes_still_needed"] = v["dataset"]["minutes_needed"]
    if v["status"] == "training":
        out["training"] = {k: v["training"][k] for k in ("epochs_done", "epochs_target",
                                                         "message", "queue_position")}
    if v["profile"]:
        p = v["profile"]
        out["profile"] = {"voice_type": p["voice_type"], "range": p["range"],
                          "vibrato": p["vibrato"], "style_caption": p["style_caption"]}
    if v["last_error"]:
        out["last_error"] = v["last_error"]
    st = v.get("style_model")
    if st:
        out["singing_style"] = {"status": st["status"], "songs_confirmed": st["songs_confirmed"],
                                "training_window": st["training"]["window"]}
    return out


def _list_voices(user, args, base):
    rows = db.query("SELECT * FROM voices WHERE user_id=? ORDER BY created_at DESC", (user.id,))
    out = {"voices": [_voice_brief(voices.voice_view(r)) for r in rows]}
    if not rows:
        out["how_to_start"] = (f"Voices are created in the browser: send the person to "
                               f"{base}/voices to create one and upload their recordings.")
    return out


def _get_voice(user, args, base):
    return _voice_brief(voices.voice_view(voices.owned(user, str(args.get("voice_id") or ""))))


def _voice_upload_link(user, args, base):
    if args.get("voice_id"):
        v = voices.owned(user, str(args["voice_id"]))
        return {"url": f"{base}/voices/{v['id']}",
                "tell_the_person": "Open this page, signed in, to upload or record your own voice. "
                                   "Only your own voice, or one you have permission to use."}
    return {"url": f"{base}/voices",
            "tell_the_person": "Open this page, signed in, to create a voice (you confirm it is "
                               "yours) and upload or record your singing."}


def _train_voice(user, args, base):
    jid = voices.start_training(user, str(args.get("voice_id") or ""),
                                str(args.get("quality") or "standard"))
    return {"job_id": jid, "status": "queued",
            "note": "Training runs in short slices on the shared GPU — usually 20-40 minutes in "
                    "all. Check with get_voice every few minutes."}


async def _wait_job(job_id: str, budget: float, started: float):
    loop = asyncio.get_running_loop()
    while True:
        row = db.one("SELECT * FROM voice_jobs WHERE id=?", (job_id,))
        if row["status"] in ("succeeded", "failed") or loop.time() - started > budget:
            return row
        await asyncio.sleep(2)


def _job_out(row, base: str) -> dict:
    j = voices.job_view(row, base)
    out = {"job_id": j["id"], "kind": j["kind"], "status": j["status"],
           "deleted_on": j["deleted_on"]}
    if j.get("result"):
        out.update({k: v for k, v in j["result"].items() if v not in (None, {}, "")})
        out["link_expires_in_hours"] = 7 * 24
        out["keep_a_copy"] = "Download it: files are deleted 90 days after they're made."
    elif j["status"] == "failed":
        out["error"] = j["error"] or "failed"
    else:
        out["message"] = j["message"]
        if j["queue_position"]:
            out["queue_position"] = j["queue_position"]
        out["next"] = "Call get_voice_job with this job_id every ~20 s until it has succeeded."
    return out


async def _resing_song(user, args, base):
    started = asyncio.get_running_loop().time()
    source = await _audio_arg(user, args, "source", 100 * 1024 * 1024)
    params = {"key": str(args.get("key", "auto")),
              "keep_backing_vocals": args.get("keep_backing_vocals", True) is not False}
    jid = await voices.use(user, str(args.get("voice_id") or ""), "resing", params, source)
    return _job_out(await _wait_job(jid, VOICE_CALL_BUDGET, started), base)


async def _sing_song(user, args, base):
    started = asyncio.get_running_loop().time()
    params = {k: args.get(k) for k in ("description", "lyrics", "style", "language",
                                       "duration_s", "seed", "use_style_model", "style_strength")
              if args.get(k) not in (None, "")}
    jid = await voices.use(user, str(args.get("voice_id") or ""), "song", params)
    return _job_out(await _wait_job(jid, VOICE_CALL_BUDGET, started), base)


async def _speak_as_voice(user, args, base):
    started = asyncio.get_running_loop().time()
    params = {k: args.get(k) for k in ("text", "language", "delivery") if args.get(k)}
    jid = await voices.use(user, str(args.get("voice_id") or ""), "speech", params)
    return _job_out(await _wait_job(jid, VOICE_CALL_BUDGET, started), base)


def _get_voice_job(user, args, base):
    return _job_out(voices.owned_job(user, str(args.get("job_id") or "")), base)


# --------------------------------------------------------------------------- podcasts
# A researched, fact-checked two-host debate that lands in the editor as a production with
# InfiniteTalk shots (app/podcast.py). The person: "our assistant should be able to help
# initiate this workflow if user simply chats through it".

def _podcast_out(row, base: str, full: bool = False) -> dict:
    from app import podcast
    v = podcast.view(row, full=True)
    out = {k: v[k] for k in ("podcast_id", "title", "topic", "status", "message", "progress",
                             "hosts", "minutes", "deleted_on") if k in v}
    out["page_url"] = f"{base}/podcast/{row['id']}"
    if v.get("error"):
        out["error"] = v["error"]
    if v.get("project_id"):
        out["project_id"] = v["project_id"]
        out["editor_url"] = base + v["editor_path"]
    out["sources_read"] = len(v.get("sources") or [])
    if v.get("orchestrator") and (full or row["status"] == "review_points"):
        out["debate_points"] = v["orchestrator"]["points"]
    if full or row["status"] == "review_research":
        for side in ("a", "b"):
            fc = v.get(f"{side}_factcheck")
            if fc:
                out[f"{side}_fact_checks"] = [f"[{x['status']}] {x['statement']}" for x in fc["verdicts"]]
    if full and v.get("script"):
        out["script"] = [{"segment": s["title"], "lines": [f"{l['speaker']}: {l['text']}" for l in s["lines"]]}
                         for s in v["script"]]
    out["next"] = {
        "review_research": "the research is ready for the person to review (page_url); when they "
                           "are happy call continue_podcast",
        "review_points": "the debate points are ready for the person to review or edit; call "
                         "continue_podcast (with `points` if they changed them)",
        "done": "it is in the editor (editor_url): the person chooses the hosts' looks there; "
                "their voices are designed and every line voiced, then the shot pictures are "
                "drawn and the InfiniteTalk shots rendered",
        "failed": "call retry_podcast to continue from where it stopped",
    }.get(row["status"], "researching and writing; check again with get_podcast in a minute or two "
                         "(a podcast takes ~10-25 minutes)")
    return out


def _make_podcast(user, args, base):
    from app import agent, podcast
    thread = str(args.pop("_thread", "") or "") or None
    if thread:
        row = db.one("SELECT user_id FROM agent_threads WHERE id=?", (thread,))
        thread = thread if row and row["user_id"] == user.id else None
    pid = podcast.create(user, args, thread_id=thread)
    return _podcast_out(db.one("SELECT * FROM podcasts WHERE id=?", (pid,)), base)


def _get_podcast(user, args, base):
    from app import podcast
    return _podcast_out(podcast.owned(user, str(args.get("podcast_id") or "")), base,
                        full=bool(args.get("full")))


def _list_podcasts(user, args, base):
    rows = db.query("SELECT * FROM podcasts WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
                    (user.id,))
    return {"podcasts": [{k: v for k, v in _podcast_out(r, base).items() if k != "next"}
                         for r in rows]}


def _continue_podcast(user, args, base):
    from app import podcast
    podcast.proceed(user, str(args.get("podcast_id") or ""), args.get("points"))
    return _get_podcast(user, args, base)


def _retry_podcast(user, args, base):
    from app import podcast
    podcast.retry(user, str(args.get("podcast_id") or ""))
    return _get_podcast(user, args, base)


def _tool(name, title, description, handler, properties=None, required=(), *,
          read_only=False, destructive=False, meta=None):
    return {
        "handler": handler,
        "spec": {
            **({"_meta": meta} if meta else {}),
            "name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": properties or {},
                            "required": list(required), "additionalProperties": False},
            "annotations": {"title": title, "readOnlyHint": read_only,
                            "destructiveHint": destructive, "openWorldHint": False},
        },
    }


TOOLS = [
    _tool("list_projects", "List projects",
          "List the signed-in person's video projects, newest first, with each one's mode "
          "(storyboard = with scenes, single = without) and whether it has a finished video.",
          _list_projects, read_only=True),
    _tool("get_project", "Get a project",
          "Everything about one project: story, settings, pictures, each scene's prompt, render "
          "status, and a download link for the finished video. Read this before regenerating.",
          _get_project, _PID, ["project_id"], read_only=True),
    _tool("create_project", "Create a project",
          "Start a new video project. It has no pictures yet; add them next.",
          _create_project,
          {"title": {"type": "string"}, "mode": _MODE,
           "story": {"type": "string",
                     "description": "Storyboard: the story to split into scenes. Single: the "
                                    "prompt itself — motion, camera and sound."}},
          ["title", "mode"]),
    _tool("update_project", "Change story or settings",
          "Change a project's title, story or settings; anything left out is kept. Setting "
          "`mode` switches the same pictures and story between WITH scenes (storyboard) and "
          "WITHOUT scenes (single) — then call render_video to regenerate it that way. "
          "Nothing is rendered by this call.",
          _update_project,
          {**_PID, "title": {"type": "string"},
           "story": {"type": "string",
                     "description": "Storyboard: raw material for plan_scenes. Single: the "
                                    "prompt sent to the model; refer to pictures as "
                                    "<Picture 1>, <Picture 2>…"},
           "mode": _MODE,
           "orientation": {"type": "string", "enum": list(service.SIZES),
                           "description": "portrait 480x864, landscape 864x480, square 640x640"},
           "seconds": {"type": "integer", "minimum": 1, "maximum": 15,
                       "description": "Single: clip length, up to 15. Storyboard: length given "
                                      "to newly planned scenes, up to 11 (existing scenes keep "
                                      "theirs). Longer values are cut to the limit."},
           "match_cuts": {"type": "boolean",
                          "description": "Storyboard only: each scene ends on the next "
                                         "scene's picture, so clips join without a jump."},
           "reference_fidelity": {"type": "string", "enum": ["match", "max"],
                                  "description": "Single only: max = stronger likeness, "
                                                 "much slower."},
           "upscale": {"type": "string", "enum": list(service.UPSCALE),
                       "description": "Experimental 2x upscale, both modes (default off). "
                                      "The video is made at the normal size and delivered at "
                                      "twice it (portrait 960x1728), sound kept. latent: about "
                                      "2.4x the render time, sharper but can invent fine "
                                      "texture. refine: about 2.7x, adds a short second pass "
                                      "and gives the cleanest detail. Applies to the next "
                                      "render."}},
          ["project_id"]),
    _tool("add_media", "Add pictures or audio by URL",
          "Add pictures (PNG/JPEG/WebP) or audio to a project from public http(s) URLs, in "
          "order. In a storyboard each picture becomes a scene; in a single clip each is a "
          "numbered reference. For a file in this chat use add_file instead.",
          _add_media,
          {**_PID, "urls": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                            "maxItems": 12}},
          ["project_id", "urls"]),
    _tool("add_file", "Add an attached or generated file",
          "Add ONE picture (PNG/JPEG/WebP) or audio file that is in this conversation — an "
          "image you generated or a file the person attached — to a project. Pass it as "
          "`file`, or pass `image_id` for a picture made with generate_image, or `speech_id` "
          "for a clip made with generate_speech, or `music_id` for a song from generate_music. "
          "Call once per "
          "file, in the order the pictures should be numbered. Use this instead of "
          "get_upload_link whenever the file lives in the chat.",
          _add_file,
          {**_PID,
           "image_id": {"type": "string",
                        "description": "A finished picture from generate_image, e.g. img_1a2b…"},
           "speech_id": {"type": "string",
                         "description": "A finished clip from generate_speech, e.g. spk_1a2b… — "
                                        "added as audio (a storyboard soundtrack)."},
           "music_id": {"type": "string",
                        "description": "A finished song from generate_music, e.g. mus_1a2b… — "
                                       "added as audio (a storyboard soundtrack)."},
           "file": {"type": "object",
                    "description": "The conversation file to add.",
                    "properties": {"download_url": {"type": "string"},
                                   "file_id": {"type": "string"}},
                    "required": ["download_url", "file_id"]},
           "base64": {"type": "string",
                      "description": "Alternative to `file`: the file's bytes, base64 "
                                     "encoded. Only practical for small files."},
           "filename": {"type": "string",
                        "description": "Optional name; its extension decides picture vs audio."}},
          ["project_id"], meta={"openai/fileParams": ["file"]}),
    _tool("get_upload_link", "Get an upload link",
          "A one-hour link that accepts picture and audio files for one project by HTTP POST, "
          "with a ready curl command — for files on a disk you can reach with a shell. For a "
          "file in this conversation (generated or attached), use add_file instead.",
          _get_upload_link, _PID, ["project_id"], read_only=True),
    _tool("plan_scenes", "Plan scenes from the story",
          "Storyboard only. Split the project's story into one scene prompt per picture, "
          "REPLACING the current scenes. Skip this if you would rather write the prompts "
          "yourself with set_scenes.",
          _plan_scenes, _PID, ["project_id"], destructive=True),
    _tool("set_scenes", "Write scene prompts",
          "Storyboard only. Set the prompt, title, length or use_case of scenes by number "
          "(scene 1 is the first picture). Only the scenes listed change. A prompt should say "
          "what moves and how the camera moves — not describe the still picture. use_case "
          "picks the model: animate (default), perform, longtalk, talk2, motion, recast, "
          "restyle; then give the scene its video (set_scene_video) or voices "
          "(set_scene_voice) when that use case needs them.",
          _set_scenes,
          {**_PID,
           "scenes": {"type": "array", "minItems": 1, "items": {
               "type": "object",
               "properties": {"scene": {"type": "integer", "minimum": 1},
                              "prompt": {"type": "string"}, "title": {"type": "string"},
                              "seconds": {"type": "integer", "minimum": 1, "maximum": 60,
                                          "description": "Scene length: up to 11 s for animate, "
                                                         "15 s motion/perform, 14 s recast/"
                                                         "restyle, 60 s talk (follows the voices)."},
                              "use_case": {"type": "string", "enum": ["animate", "perform",
                                           "longtalk", "talk2", "motion", "recast", "restyle"],
                                           "description": "What happens in this scene; each "
                                           "is made by the model built for it (see the server "
                                           "instructions)."},
                              "characters": {"type": "array", "items": {"type": "string"},
                                             "description": "the characters/places/objects in this "
                                                            "scene, by name (set_characters)"},
                              "wear": {"type": "object", "description": "what a character wears/"
                                       "carries in THIS scene when it differs from their usual: "
                                       "{\"Abhi\": [\"Abhi's kurta\"]}; null goes back to usual"},
                              "opts": {"type": "object", "description": "layout (talk2), who "
                                       "(recast: who is replaced), change (restyle)",
                                       "properties": {"layout": {"type": "string", "enum": [
                                           "left_right", "right_left"]}, "who": {"type": "string"},
                                           "change": {"type": "string"}},
                                       "additionalProperties": False}},
               "required": ["scene"]}}},
          ["project_id", "scenes"]),
    _tool("set_scene_video", "Give a scene its video",
          "Storyboard only. The video a scene works from, for use_case motion (copy its "
          "movements), recast (replace a person in it), restyle (change its look) or perform "
          "(a loose motion/camera guide). Either another scene's rendered clip (from_scene) "
          "or a public video URL (video_url, up to 95 MB).",
          _set_scene_video,
          {**_PID, "scene": {"type": "integer", "minimum": 1},
           "from_scene": {"type": "integer", "minimum": 1},
           "video_url": {"type": "string"}},
          ["project_id", "scene"]),
    _tool("set_scene_voice", "Give a scene its voice or song",
          "Storyboard only. For use_case longtalk (one voice or song, up to 60 s) or talk2 "
          "(speaker 1, then speaker 2). From a finished generate_speech result (speech_id), a "
          "generate_music result (music_id) or a public audio URL. The scene's length follows "
          "the voices.",
          _set_scene_voice,
          {**_PID, "scene": {"type": "integer", "minimum": 1},
           "speaker": {"type": "integer", "enum": [1, 2], "description": "talk2: 1 or 2"},
           "speech_id": {"type": "string"}, "music_id": {"type": "string"},
           "audio_url": {"type": "string"}},
          ["project_id", "scene"]),
    _tool("render_video", "Generate or regenerate the video",
          "Queue a render of the project as it now stands, replacing its previous video. "
          "Storyboard: renders every scene and joins them, or pass `scenes` to regenerate "
          "only those and keep the other clips. Single: renders the one clip. Returns "
          "immediately — poll get_status. Uses real GPU time and counts against the "
          "account's hourly render limit.",
          _render_video,
          {**_PID, "scenes": {"type": "array", "items": {"type": "integer", "minimum": 1},
                              "description": "Storyboard only: scene numbers to regenerate. "
                                             "Omit to regenerate the whole video."}},
          ["project_id"]),
    _tool("get_status", "Check a render",
          "Render progress for a project, per scene, and the video download link once it has "
          "succeeded.",
          _get_status, _PID, ["project_id"], read_only=True),
    _tool("cancel_render", "Cancel a render",
          "Stop a queued or running render and free the GPU. The previous video, if any, is kept.",
          _cancel_render, _PID, ["project_id"]),
    _tool("generate_image", "Generate a picture",
          "Make a still picture on the studio's GPU. With only a prompt it is text-to-image. "
          "With source pictures it is image-to-image: the prompt says what to change, or how "
          "to combine them (\"the person in picture 1 standing in the room from picture 2\"). "
          "Up to 4 sources, from any mix of `file`, `base64`, `image_ids`, project "
          "`pictures` and `urls` — numbered in THAT order, which is what \"picture 1\" in "
          "the prompt refers to. An edit with no size given comes back at the first "
          "source's size. `model` picks the generator: flux-2-klein-9b (default), or "
          "qwen-image-2.1-turbo / qwen-image-2.1 — Qwen-Image-2.1, one model that draws AND "
          "edits, notably good at lettering and text in pictures. "
          "Usually returns the finished picture within the call; if `status` is not "
          "`succeeded`, call get_image. Counts against the account's hourly image limit.",
          _generate_image,
          {"prompt": {"type": "string",
                      "description": "What to draw, or for image-to-image, what to change."},
           "model": {"type": "string", "enum": list(images.MODELS),
                     "description": "; ".join(f"{k}: {v}" for k, v in images.MODELS.items())},
           "orientation": {"type": "string", "enum": list(images.SIZES),
                           "description": "square 1024x1024, portrait 864x1536, landscape "
                                          "1536x864. For a storyboard scene, match the "
                                          "project's orientation."},
           "width": {"type": "integer", "minimum": images.MIN_SIDE,
                     "maximum": images.MAX_SIDE,
                     "description": "Exact size instead of orientation; give height too."},
           "height": {"type": "integer", "minimum": images.MIN_SIDE,
                      "maximum": images.MAX_SIDE},
           "seed": {"type": "integer", "minimum": 0,
                    "description": "Repeat a result exactly with the same prompt and seed."},
           "file": {"type": "object",
                    "description": "Image-to-image: a picture in this conversation.",
                    "properties": {"download_url": {"type": "string"},
                                   "file_id": {"type": "string"}},
                    "required": ["download_url", "file_id"]},
           "base64": {"type": "string",
                      "description": "Image-to-image: a picture's bytes, base64 encoded."},
           "image_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 4,
                         "description": "Image-to-image: earlier results from "
                                        "generate_image — pass the last one to refine it."},
           "urls": {"type": "array", "items": {"type": "string"}, "maxItems": 4,
                    "description": "Image-to-image: public http(s) links to pictures."},
           "project_id": {"type": "string",
                          "description": "Only with `pictures`: the project they are in."},
           "pictures": {"type": "array", "items": {"type": "integer", "minimum": 1},
                        "maxItems": 4,
                        "description": "Image-to-image: picture numbers in project_id."}},
          ["prompt"], meta={"openai/fileParams": ["file"]}),
    _tool("get_image", "Check a picture",
          "Status of a picture from generate_image; once it has succeeded, the picture "
          "itself and a download link.",
          _get_image,
          {"image_id": {"type": "string", "description": "e.g. img_1a2b…"}},
          ["image_id"], read_only=True),
    _tool("generate_speech", "Speak text aloud",
          "Turn text into speech with Qwen3-TTS on the studio's GPU. By default a preset "
          "`speaker` reads it (Ryan if none given), optionally steered with `instruct`. To "
          "speak in someone else's voice instead, pass ONE reference clip (`file`, "
          "`reference_base64` or `reference_url`, 5-30 s of clear speech). BURMESE: text in Burmese "
          "script (or language=Burmese) is spoken by a separate engine, VoxCPM2, with its own "
          "speakers Thiri (default), Nandar, Kyaw and U_Ba; the other speakers cannot speak "
          "Burmese and these four speak only Burmese. A clone can speak Burmese whatever "
          "language the clip is in; for Burmese the clone uses the audio only, so "
          "reference_text, match and temperature have no effect. HINDI: text in Devanagari "
          "(or language=Hindi) has two choices of voice. The default is one of four native "
          "Hindi speakers on VoxCPM2: Meera (default), Anaya, Arjun, Vikram; they speak only "
          "Hindi and pronounce it like native speakers. Any of the nine Qwen speakers (Ryan, "
          "Serena, ...) can also be picked for Hindi: clear, but with a noticeable foreign "
          "accent. Hindi clones run on VoxCPM2 like Burmese ones. SANSKRIT ŚLOKA CHANT: "
          "language=Sanskrit (or speaker=Vagdhenu) CHANTS the text as a traditional "
          "recitation with Vāgdhenu, a metre-aware model with one voice. It is never "
          "auto-detected, because Hindi and Sanskrit share Devanagari. Pass the verse in any "
          "Indian script or IAST; several ślokas (each ending in ॥) are chanted one by one and "
          "joined. The metre is detected unless `meter` is given; the result's message names "
          "the metre used. No cloning and no `instruct`. By default "
          "(match=style) the clone copies the speaker's accent, pace and delivery: that needs "
          "the clip's exact words, which are transcribed automatically if `reference_text` is "
          "not given (pass it when you know it — accuracy matters). match=timbre copies only "
          "the voice colour. `takes` (1-3) makes variations to choose from; `seed` repeats "
          "one. Up to 1200 characters per clip — split longer text. "
          "Usually returns the finished clip within the call; if `status` is not "
          "`succeeded`, call get_speech. While the GPU is busy with pictures or videos the "
          "clip waits in a queue (the reply says its position) and runs when the card is "
          "free. Counts against the account's hourly speech limit.",
          _generate_speech,
          {"text": {"type": "string", "maxLength": speech.MAX_TEXT,
                    "description": "The words to speak."},
           "speaker": {"type": "string", "enum": list(speech.SPEAKERS),
                       "description": "Preset voice. " + "; ".join(
                           f"{k} ({v})" for k, v in speech.SPEAKERS.items()) +
                       ". The first nine (Qwen) speak every language except Burmese, Hindi with "
                       "a foreign accent; Thiri, Nandar, Kyaw and U_Ba speak only Burmese; "
                       "Meera, Anaya, Arjun and Vikram speak only Hindi, natively; Vagdhenu "
                       "chants Sanskrit ślokas only."},
           "meter": {"type": "string", "enum": speech.METERS,
                     "description": "Sanskrit chant only: the chandas to chant in. auto "
                                    "(default) detects it from the verse; anuṣṭubh is the "
                                    "common śloka metre (e.g. the Gītā)."},
           "language": {"type": "string", "enum": speech.LANGUAGES,
                        "description": "Language of the text; Auto detects it (Burmese "
                                       "script goes to the Burmese voices; Devanagari is Hindi, "
                                       "spoken by Meera unless a speaker is given)."},
           "instruct": {"type": "string", "maxLength": speech.MAX_INSTRUCT,
                        "description": "Preset speakers only: how to say it, e.g. \"warm and "
                                       "slow, like a bedtime story\" or \"angry, shouting\"."},
           "file": {"type": "object",
                    "description": "Voice clone: an audio clip in this conversation.",
                    "properties": {"download_url": {"type": "string"},
                                   "file_id": {"type": "string"}},
                    "required": ["download_url", "file_id"]},
           "reference_base64": {"type": "string",
                                "description": "Voice clone: an audio clip's bytes, base64."},
           "reference_url": {"type": "string",
                             "description": "Voice clone: a public http(s) link to a clip."},
           "reference_text": {"type": "string",
                              "description": "Voice clone: the exact words spoken in the "
                                             "reference clip. Transcribed automatically "
                                             "when omitted."},
           "match": {"type": "string", "enum": list(speech.MATCHES),
                     "description": "Voice clone: style (default) = accent, pace and delivery "
                                    "too; timbre = voice colour only."},
           "temperature": {"type": "number", "minimum": 0.1, "maximum": 1.5,
                           "description": "Expressiveness / variation. Clones default to "
                                          "0.7; lower is flatter, higher is livelier."},
           "seed": {"type": "integer", "minimum": 0,
                    "description": "Repeat a take exactly (same text, voice and settings)."},
           "takes": {"type": "integer", "minimum": 1, "maximum": speech.MAX_TAKES,
                     "description": "How many variations to make (each counts toward the "
                                    "hourly limit)."}},
          ["text"], meta={"openai/fileParams": ["file"]}),
    _tool("get_speech", "Check a speech clip",
          "Status of a clip from generate_speech, and its audio link once it has succeeded.",
          _get_speech,
          {"speech_id": {"type": "string", "description": "e.g. spk_1a2b…"}},
          ["speech_id"], read_only=True),
    _tool("list_speech", "List speech clips",
          "The signed-in person's 30 most recent clips from generate_speech, newest first, "
          "with their text and audio links.",
          _list_speech, read_only=True),
    _tool("generate_music", "Make a song",
          "Make music with ACE-Step 1.5 on the studio's GPU. `task` picks what to do:\n"
          "- describe: one sentence in `description`; the model writes lyrics, style and "
          "structure itself.\n"
          "- custom (default): your `lyrics` — use [Verse], [Chorus], [Bridge], [Intro], "
          "[Outro] tags on their own lines — plus a `style` (genre, instruments, mood, vocal "
          "type, tempo: \"female vocal, acoustic folk, fingerpicked guitar, warm, 90 bpm\"). "
          "Set instrumental=true for no vocals. A reference_* track steers the sound.\n"
          "- cover: re-perform source_* in a new `style` (required) — the original words "
          "are kept unless you give new `lyrics`; `strength` 0-1 = how closely to follow "
          "the source (default 0.5; 0.3 = big genre change, 0.8 = subtle).\n"
          "- repaint: regenerate seconds `start`..`end` (3-90 s) of source_*; the rest is "
          "kept. `style`/`lyrics` default to the source song's.\n"
          "- extract: pull one stem (`track`) out of source_*.\n"
          "- add_layer: generate a new stem (`track`) that fits source_*; `style` "
          "describes only that new part (required).\n"
          "- extend: make source_* longer by `add_seconds` (10-120, default 30); optional "
          "`lyrics` are sung in the new part.\n"
          "- accompany: build `instruments` around a single track (e.g. a band under an "
          "a-cappella vocal); `style` describes the arrangement (required).\n"
          "For an uploaded source with no `lyrics`, the words are transcribed from it.\n"
          "Usually 20-90 s once it starts. While the GPU is busy with pictures or videos it "
          "waits in a queue; the reply gives queue_position — then poll get_music every ~20 s. "
          "Give the person the audio_url. Counts against the account's hourly music limit.",
          _generate_music,
          {"task": {"type": "string", "enum": list(music.TASKS)},
           "description": {"type": "string", "maxLength": music.MAX_PROMPT,
                           "description": "describe: what the song should be, e.g. \"an "
                                          "upbeat Hindi wedding song about the monsoon\"."},
           "lyrics": {"type": "string", "maxLength": music.MAX_LYRICS,
                      "description": "The words to sing, with [Verse]/[Chorus] section tags."},
           "style": {"type": "string", "maxLength": music.MAX_PROMPT,
                     "description": "Genre, instruments, mood, vocal type, tempo."},
           "instrumental": {"type": "boolean", "description": "No vocals."},
           "language": {"type": "string", "enum": list(music.LANGUAGES),
                        "description": "Language the lyrics are sung in (code). unknown = auto."},
           "duration": {"type": "number", "minimum": music.MIN_S, "maximum": music.MAX_S,
                        "description": "Length in seconds; omit to let the model choose."},
           "bpm": {"type": "integer", "minimum": 30, "maximum": 300},
           "key": {"type": "string", "description": "e.g. \"C major\", \"A minor\"."},
           "time_signature": {"type": "string", "enum": ["2", "3", "4", "6"],
                              "description": "Beats per bar: 2, 3, 4, or 6 (6/8)."},
           "seed": {"type": "integer", "minimum": 0,
                    "description": "Repeat a result with the same inputs and seed."},
           "strength": {"type": "number", "minimum": 0, "maximum": 1,
                        "description": "cover: how closely to follow the source (default 0.5)."},
           "start": {"type": "number", "minimum": 0, "description": "repaint: from (s)."},
           "end": {"type": "number", "minimum": 0, "description": "repaint: to (s)."},
           "add_seconds": {"type": "number", "minimum": music.MIN_ADD_S,
                           "maximum": music.MAX_ADD_S,
                           "description": "extend: how many seconds to add (default 30)."},
           "track": {"type": "string", "enum": music.TRACKS,
                     "description": "extract / add_layer: which stem."},
           "instruments": {"type": "array", "items": {"type": "string", "enum": music.TRACKS},
                           "description": "accompany: instruments to build around the track."},
           "title": {"type": "string", "description": "Optional name for the song."},
           "source_music_id": {"type": "string", "description": "source track for cover/repaint/extract/add_layer/extend/accompany: an earlier song from generate_music."},
           "source_speech_id": {"type": "string", "description": "source track for cover/repaint/extract/add_layer/extend/accompany: a clip from generate_speech."},
           "source_file": {"type": "object", "description": "source track for cover/repaint/extract/add_layer/extend/accompany: an audio file in this conversation.",
                    "properties": {"download_url": {"type": "string"}, "file_id": {"type": "string"}},
                    "required": ["download_url", "file_id"]},
           "source_base64": {"type": "string", "description": "source track for cover/repaint/extract/add_layer/extend/accompany: audio bytes, base64."},
           "source_url": {"type": "string", "description": "source track for cover/repaint/extract/add_layer/extend/accompany: a public http(s) link to audio."},
           "reference_music_id": {"type": "string", "description": "Style reference (custom/describe): an earlier song from generate_music."},
           "reference_speech_id": {"type": "string", "description": "Style reference (custom/describe): a clip from generate_speech."},
           "reference_file": {"type": "object", "description": "Style reference (custom/describe): an audio file in this conversation.",
                    "properties": {"download_url": {"type": "string"}, "file_id": {"type": "string"}},
                    "required": ["download_url", "file_id"]},
           "reference_base64": {"type": "string", "description": "Style reference (custom/describe): audio bytes, base64."},
           "reference_url": {"type": "string", "description": "Style reference (custom/describe): a public http(s) link to audio."},
          },
          [], meta={"openai/fileParams": ["source_file", "reference_file"]}),
    _tool("get_music", "Check a song",
          "Status of a song from generate_music — queue position while waiting; once it has "
          "succeeded, the audio link, the final lyrics and the bpm/key it was made in.",
          _get_music,
          {"music_id": {"type": "string", "description": "e.g. mus_1a2b…"}},
          ["music_id"], read_only=True),
    _tool("list_music", "List songs",
          "The signed-in person's 30 most recent songs from generate_music, newest first.",
          _list_music, read_only=True),
    _tool("list_images", "List pictures",
          "The signed-in person's 30 most recent pictures from generate_image, newest first, "
          "with prompts and download links.",
          _list_images, read_only=True),
    _tool("list_voices", "List my voices",
          "The person's own trained voices (made from their own recordings), with status, how "
          "much audio they have, and the measured voice profile (range, vibrato, tone). Voices "
          "are private to their owner and are kept until deleted.",
          _list_voices, read_only=True),
    _tool("get_voice", "Get a voice", "One voice: status, training progress, profile.",
          _get_voice, {"voice_id": {"type": "string"}}, ["voice_id"], read_only=True),
    _tool("get_voice_upload_link", "Link to add recordings",
          "A page where the PERSON creates a voice and uploads or records their own voice in "
          "their browser. Give them the URL. Never upload someone's voice for them.",
          _voice_upload_link, {"voice_id": {"type": "string",
                                            "description": "omit to create a new voice"}},
          read_only=True),
    _tool("train_voice", "Train a voice",
          "Start training once the person has uploaded at least 5 minutes of singing (10-30 "
          "sounds fuller). Runs in slices on the shared GPU (20-40 min). Poll get_voice.",
          _train_voice, {"voice_id": {"type": "string"},
                         "quality": {"type": "string", "enum": ["standard", "high"]}},
          ["voice_id"]),
    _tool("resing_song", "Change the singer to my voice",
          "Re-sing a song's lead vocal in the person's trained voice and mix it back onto the "
          "original music. key=auto moves the vocal by octaves into their range. It keeps the "
          "original melody and phrasing. Give the song as one source_*.",
          _resing_song, {"voice_id": {"type": "string"},
                         "key": {"type": "string", "description": "auto, or semitones -24..24"},
                         "keep_backing_vocals": {"type": "boolean"},
                         "source_music_id": {"type": "string", "description": "a song from generate_music"},
                         "source_file": {"type": "object", "properties": {"download_url": {"type": "string"},
                                         "file_id": {"type": "string"}}, "required": ["download_url", "file_id"]},
                         "source_url": {"type": "string", "description": "public http(s) audio link"},
                         "source_base64": {"type": "string"}},
          ["voice_id"]),
    _tool("sing_song", "Sing a new song in my voice",
          "Write (from `description`) or take (`lyrics` + optional `style`) a song, perform it in "
          "the person's range and manner from their voice profile, and sing it in their trained "
          "voice. About 1-2 minutes when the GPU is free.",
          _sing_song, {"voice_id": {"type": "string"},
                       "description": {"type": "string", "maxLength": 1000},
                       "lyrics": {"type": "string", "maxLength": 4000},
                       "style": {"type": "string", "maxLength": 1000},
                       "language": {"type": "string"},
                       "duration_s": {"type": "number", "minimum": 10, "maximum": 360},
                       "seed": {"type": "integer", "minimum": 0},
                       "use_style_model": {"type": "boolean", "description": "perform it with the person's trained singing style when they have one (default true)"},
                       "style_strength": {"type": "number", "minimum": 0, "maximum": 1, "description": "how strongly (default 0.8)"}},
          ["voice_id"]),
    _tool("speak_as_voice", "Speak text in my voice",
          "Say text in the person's trained voice. Burmese (Myanmar) and Hindi text work too: "
          "they are detected automatically (or pass language=Burmese / language=Hindi) and "
          "spoken by the VoxCPM2 engine before the person's timbre is applied.",
          _speak_as_voice, {"voice_id": {"type": "string"},
                            "text": {"type": "string", "maxLength": 2000},
                            "language": {"type": "string"},
                            "delivery": {"type": "string", "description": "e.g. calm and slow"}},
          ["voice_id", "text"]),
    _tool("list_characters", "List characters",
          "Storyboard only. The project's characters, places, props (outfits, objects) and styles: "
          "description, picture (picture_url), what goes with them, their voice, and whether the "
          "person has chosen the look.",
          _list_characters, _PID, ["project_id"], read_only=True),
    _tool("set_characters", "Add or change characters",
          "Storyboard only. Add or change characters, places, props and styles by name. Give each "
          "character the story's own description (age, skin, hair, clothes), a `voice` brief "
          "(gender, age, pitch, timbre, pace, accent) -- their own voice is designed from it -- "
          "and `goes_with`: the props (outfit, objects) and style that always come with them. A "
          "picture the person gives (image_id or picture_url) is their look; a character sheet "
          "(several views side by side) is cut into views automatically.",
          _set_characters,
          {**_PID, "characters": {"type": "array", "items": {"type": "object", "properties": {
              "name": {"type": "string"},
              "kind": {"type": "string", "enum": ["character", "location", "prop", "style", "voice"]},
              "description": {"type": "string"}, "voice": {"type": "string"},
              "goes_with": {"type": "array", "items": {"type": "string"}},
              "image_id": {"type": "string"}, "picture_url": {"type": "string"},
              "speaker": {"type": "string", "description": "a preset voice instead of a designed one"},
              "rename": {"type": "string"}},
              "required": ["name"], "additionalProperties": False}},
           "remove": {"type": "array", "items": {"type": "string"}}},
          ["project_id"]),
    _tool("draw_characters", "Draw characters",
          "Storyboard only. Pictures for the characters, places, props and styles that have none "
          "(or `names`, or `again` to redraw). Then show them to the person and ask them to "
          "choose: nothing else is made with a character until choose_looks.",
          _draw_characters,
          {**_PID, "names": {"type": "array", "items": {"type": "string"}},
           "again": {"type": "boolean"}, "prompt": {"type": "string"}, "model": {"type": "string"}},
          ["project_id"]),
    _tool("choose_looks", "Choose characters' looks",
          "Record that the person has chosen these characters' looks (omit `characters` for all "
          "of them). Call it ONLY when the person said so. Until then no scene picture, voice or "
          "video is made with a character.",
          _choose_looks,
          {**_PID, "characters": {"type": "array", "items": {"type": "string"}}}, ["project_id"]),
    _tool("design_voice", "Design a character's voice",
          "Design (or redesign) a character's own voice from a voice brief (or their "
          "description). Their lines are voiced again in it.",
          _design_voice,
          {**_PID, "character": {"type": "string"}, "voice": {"type": "string"}},
          ["project_id", "character"]),
    _tool("edit_picture", "Edit a picture",
          "Change something in a scene's picture (scene) or a character's picture (character) "
          "and keep everything else: say only what changes ('she is not smiling', 'evening "
          "light'). Returns options; use_picture puts the chosen one in place.",
          _edit_picture,
          {**_PID, "scene": {"type": "integer", "minimum": 1}, "character": {"type": "string"},
           "instruction": {"type": "string"}, "count": {"type": "integer", "minimum": 1, "maximum": 3},
           "model": {"type": "string"}},
          ["project_id", "instruction"]),
    _tool("use_picture", "Use a picture",
          "Put an image (an edit_picture option, a generate_image result) in as a scene's picture "
          "or a character's picture (that also chooses the character's look).",
          _use_picture,
          {**_PID, "image_id": {"type": "string"}, "scene": {"type": "integer", "minimum": 1},
           "character": {"type": "string"}},
          ["project_id", "image_id"]),
    _tool("draw_scene_pictures", "Draw scene pictures",
          "Storyboard only. Draw each scene's picture (or `scenes`) from its characters, their "
          "outfits and its place: two options per scene, the better one kept. Runs in the "
          "background; get_status shows progress. No video is made.",
          _draw_scene_pictures,
          {**_PID, "scenes": {"type": "array", "items": {"type": "integer"}}}, ["project_id"]),
    _tool("share_to_batiyao", "Share to Batiyao",
          "Share a finished item to the person's Batiyao: kind project (its film), image, speech "
          "or music. mode post (text up to 500 characters, no #hashtags; COSTS the person 1.0 "
          "BuckBuck -- confirm_spend only after they agreed) or story (free; a picture or a video "
          "up to 30 s; safe for everyone).",
          _share_to_batiyao,
          {"kind": {"type": "string", "enum": ["project", "image", "speech", "music"]},
           "id": {"type": "string"}, "mode": {"type": "string", "enum": ["post", "story"]},
           "text": {"type": "string"}, "rating": {"type": "string", "enum": ["sfw", "suggestive", "nsfw"]},
           "confirm_spend": {"type": "boolean"}},
          ["kind", "id", "mode"]),
    _tool("make_podcast", "Make a podcast",
          "Make a two-host DEBATE PODCAST about a topic, end to end: research agents read the web "
          "for each side, fact-checkers check every claim against fresh sources, a neutral "
          "orchestrator keeps the points where the hosts really collide, and the script is written "
          "segment by segment. It lands in the studio's editor as a new production: the two hosts "
          "as characters (the person chooses their looks; voices designed from the description), "
          "a scene per segment, and InfiniteTalk talking shots with every line under them. Takes "
          "~10-25 minutes; returns podcast_id at once. Use it whenever the person asks for a "
          "podcast, debate, discussion show or two-person conversation episode. FIT THE SIDES TO "
          "THE TOPIC with hosts[].lens: a comparison (X vs Y) gets one host arguing for X and one "
          "for Y; a yes/no question one for and one against; left vs right (the default, hosts "
          "Elena Vance and Marcus Sterling) only for political policy questions. Give each host a "
          "name, look and voice that suit their side (e.g. an Indian host arguing for India). "
          "Defaults: about 8 minutes, 16:9.",
          _make_podcast,
          {"topic": {"type": "string", "description": "The question or topic they debate."},
           "title": {"type": "string"},
           "minutes": {"type": "number", "description": "3-30, default 8."},
           "lenses": {"type": "string", "enum": ["left_right", "optimist_skeptic", "reform_tradition"],
                      "description": "The two sides, unless hosts[].lens says otherwise."},
           "hosts": {"type": "array", "maxItems": 2, "items": {"type": "object", "properties": {
               "name": {"type": "string"}, "lens": {"type": "string", "description": "Their side or worldview."},
               "look": {"type": "string", "description": "Appearance: gender, age, hair, clothes."},
               "voice": {"type": "string", "description": "Voice description: gender, age, accent, manner."}}}},
           "aspect": {"type": "string", "enum": ["16:9", "9:16", "1:1"]},
           "language": {"type": "string", "description": "Language of the script, default English."},
           "focus": {"type": "string", "description": "Research focus or constraints."},
           "review": {"type": "array", "items": {"type": "string", "enum": ["research", "points"]},
                      "description": "Stop for the person to review these first (default: none)."}},
          ["topic"]),
    _tool("get_podcast", "Check a podcast",
          "Where a podcast run stands (status, progress, message); at a review gate the research "
          "or debate points; when done, project_id and editor_url. full=true adds the fact-checks, "
          "debate points and script.",
          _get_podcast, {"podcast_id": {"type": "string"}, "full": {"type": "boolean"}},
          ["podcast_id"], read_only=True),
    _tool("list_podcasts", "List podcasts", "The person's podcast runs, newest first.",
          _list_podcasts, read_only=True),
    _tool("continue_podcast", "Continue a podcast",
          "Continue a podcast waiting for review (status review_research or review_points). At "
          "review_points, `points` replaces the debate points (each: title, debateQuestion, "
          "aPosition, bPosition, factualGround, philosophicalVulnerability).",
          _continue_podcast,
          {"podcast_id": {"type": "string"},
           "points": {"type": "array", "items": {"type": "object"}}}, ["podcast_id"]),
    _tool("retry_podcast", "Retry a podcast",
          "Continue a failed or cancelled podcast from where it stopped (finished steps are kept).",
          _retry_podcast, {"podcast_id": {"type": "string"}}, ["podcast_id"]),
    _tool("get_voice_job", "Check a voice job",
          "A resing/song/speech job: when it has succeeded, audio_url and stems (links work for "
          "7 days; files are deleted after 90 days).",
          _get_voice_job, {"job_id": {"type": "string"}}, ["job_id"], read_only=True),
]
_BY_NAME = {t["spec"]["name"]: t for t in TOOLS}


# --------------------------------------------------------------------------- protocol

def _result(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


async def _call_tool(user: auth.User, params: dict, base: str) -> dict:
    tool = _BY_NAME.get(params.get("name"))
    if tool is None:
        raise KeyError(params.get("name"))
    args = params.get("arguments") or {}
    try:
        out = tool["handler"](user, args if isinstance(args, dict) else {}, base)
        if inspect.isawaitable(out):
            out = await out
        preview = out.pop("_preview", None) if isinstance(out, dict) else None
        content = [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=1)}]
        if preview:
            # So the assistant can SEE what it made, and the person sees it in
            # the chat. A small JPEG: the full PNG is behind image_url.
            content.append({"type": "image", "mimeType": "image/jpeg",
                            "data": base64.b64encode(preview).decode()})
        return {"content": content}
    except HTTPException as exc:
        # A refusal is an answer, not a protocol fault: the assistant should
        # read "plan the shots first" and act on it, so it goes back as the
        # tool's own output with isError set.
        return {"content": [{"type": "text", "text": str(exc.detail)}], "isError": True}
    except Exception:                                          # noqa: BLE001
        log.exception("tool %s failed", params.get("name"))
        return {"content": [{"type": "text", "text": "the studio hit an internal error"}],
                "isError": True}


async def _handle(user: auth.User, msg, base: str) -> dict | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(None, -32600, "not a JSON-RPC 2.0 message")
    method, msg_id = msg.get("method"), msg.get("id")
    if method is None or "id" not in msg:
        return None                      # a notification, or a response we never asked for
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "initialize":
        wanted = params.get("protocolVersion")
        log.info("user %s initialized (%s, protocol %s)", user.username,
                 (params.get("clientInfo") or {}).get("name"), wanted)
        return _result(msg_id, {
            "protocolVersion": wanted if wanted in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "story-studio", "title": "Story Studio", "version": "1.2.0"},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        # Logged so "my client still shows the old tools" can be told apart
        # from a server that never got asked.
        log.info("user %s listed tools (%d)", user.username, len(TOOLS))
        return _result(msg_id, {"tools": [t["spec"] for t in TOOLS]})
    if method == "tools/call":
        try:
            return _result(msg_id, await _call_tool(user, params, base))
        except KeyError as exc:
            return _error(msg_id, -32602, f"unknown tool {exc}")
    return _error(msg_id, -32601, f"method not found: {method}")


def _unauthorized(request: Request) -> JSONResponse:
    # The header is the whole discovery mechanism: it is how ChatGPT and Claude
    # learn where to send the person to sign in.
    return JSONResponse(
        {"error": "invalid_token",
         "error_description": "Sign in to Story Studio to use this server. "
                              f"See {service.base_url(request)}/install"},
        status_code=401,
        headers={"WWW-Authenticate":
                 f'Bearer resource_metadata="{oauth.resource_metadata_url(request)}"'})


@router.post("/mcp")
async def mcp_post(request: Request):
    user = oauth.user_for_request(request)
    if user is None:
        return _unauthorized(request)
    try:
        body = json.loads(await request.body())
    except ValueError:
        return JSONResponse(_error(None, -32700, "parse error"), status_code=400)
    base = service.base_url(request)
    if isinstance(body, list):
        answers = [a for a in [await _handle(user, m, base) for m in body] if a is not None]
        return JSONResponse(answers) if answers else Response(status_code=202)
    answer = await _handle(user, body, base)
    return JSONResponse(answer) if answer is not None else Response(status_code=202)


@router.get("/mcp")
@router.delete("/mcp")
async def mcp_other(request: Request):
    if oauth.user_for_request(request) is None:
        return _unauthorized(request)
    # No server-initiated stream and no session to end; the spec's answer for
    # both is 405, and clients carry on with POST alone.
    return Response(status_code=405, headers={"Allow": "POST"})
