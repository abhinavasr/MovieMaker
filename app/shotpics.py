"""Shot pictures, planned and checked by the local 27B (2026-11-25).

The person: "while the story lines and shots are decent, the default images that are
getting generated for shots are very bad ... take learnings to understand how this can be
improved". What the pictures of "Mobile Runner" showed:

* every shot was the PLACE picture with the characters pasted in: FLUX.2 klein copies a
  source's layout, so every shot had the same camera position and the pole dead centre;
* characters stood facing the camera like their full-body reference sheets, instead of
  acting the moment (him spotting her AHEAD, sprinting, crashing);
* every character of the scene was sent to every shot, so a close-up of him had her in it,
  and the framing word was lost in a run-on prompt written for VIDEO ("camera pushes in");
* the two "options" were the same model and near-identical;
* nothing looked at the result -- the person had to ("draw new with correct context and
  validate images").

So: the planner model writes each picture as a STILL (the shot's first frame, framing
honoured, who is where, pose mid-action, the setting detail that matters) and lists who is
actually visible; only those characters go in as sources, face close-up first; the place
picture only for wide shots and labelled as a look, not a layout; options use different
models and seeds; and the planner LOOKS at the options against checks it wrote and says
which fits best.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
from pathlib import Path

from app import db, images
from app.config import cfg

log = logging.getLogger("studio.shotpics")

FRAMING = {"EWS": "extreme wide shot: the whole place, people small in it",
           "WS": "wide shot: people seen head to toe with plenty of the place around them",
           "MS": "medium shot: people from the waist up",
           "MCU": "medium close-up: head and shoulders",
           "CU": "close-up: the face fills most of the frame",
           "ECU": "extreme close-up: one detail (eyes, a hand) fills the frame",
           "OTS": "over-the-shoulder shot: the back of one person's shoulder and head in the "
                  "foreground, the other person facing the camera",
           "POV": "point-of-view shot: what the character sees, from their eyes",
           "2S": "two-shot: two people together in the frame",
           "INSERT": "insert shot: a close view of an object or detail"}
WIDE = {"EWS", "WS", "2S", ""}

PLAN_RULES = """You plan ONE still picture: the FIRST FRAME of a film shot. A video model will
animate it afterwards, so draw the moment the shot STARTS, like a film still or a photograph --
BEFORE the shot's main event happens (2026-11-25, the person: "it should always be the first
frame for shots"). If the shot is "he walks along, then slams into a pole", draw him walking
along with the pole ahead -- never the impact; the video makes the event happen.
Write `prompt` as a description of that single image, 50-110 words, present tense:
- the framing exactly as asked (a close-up means the face fills the frame; do not widen it);
- who is visible and WHERE: left/right/centre, foreground/background, how far, which way each
  faces and looks (e.g. "she walks toward camera in the distance; he is on the right, seen
  from behind, looking at her");
- each visible person's pose mid-action and expression at that instant;
- the setting details that matter to the story (e.g. "on the raised concrete sidewalk beside
  the road, not on the road"), the light, the time of day;
- the film's style (e.g. photorealistic) as given;
- each visible person in EXACTLY the clothes, accessories and hair their cast entry gives (and
  what it says they wear or carry): never invent, swap or recolour an outfit between shots
  (2026-11-25: the clothes kept changing from scene to scene).
Never write camera movement, shot titles, sound, dialogue or on-screen text. Never add people
who are not in the shot. `visible`: the exact names (from the cast list) of the characters who
can be seen in THIS frame -- only them. `place`: the name of the place it happens in, or null.
`checks`: 3-5 short yes/no checks anyone can verify by looking at the picture (framing, who is
present, where they are, the key setting detail, the key action). Answer with JSON only."""

PLAN_SCHEMA = {"type": "object", "properties": {
    "prompt": {"type": "string"}, "visible": {"type": "array", "items": {"type": "string"}},
    "place": {"type": ["string", "null"]}, "checks": {"type": "array", "items": {"type": "string"}}},
    "required": ["prompt", "visible", "checks"]}

JUDGE_RULES = """You check pictures made for a film shot. Each picture is the shot's FIRST FRAME:
the moment the shot starts, BEFORE its main event (an impact, a reaction, an arrival) happens --
never mark a picture down because that event has not happened yet. For each picture, score 0-10
how well it matches the checks and the framing, who is in it and where, the setting, natural
anatomy (count hands, limbs and faces; no extra people) and a natural, not pasted-in, look.
IDENTITY COMES FIRST: when reference cards of the characters are attached after the pictures,
each person must be recognisably THE SAME person as their card (face shape, eyes, nose, hair,
skin, age, build). A different-looking person scores 3 at most, however good the picture is.
Also check: it is ONE single photograph (a collage, split screen, panels or a reference sheet
scores 2 at most); the right number of people; any written text (signs, screens) spelled exactly
as the shot says, or not readable at all; and, when this camera set-up's MASTER frame is
attached, the same room, camera position and people as the master.
List the checks it fails, briefly. For each picture also give `fix`: ONE short instruction that
would repair it by editing just that part while keeping everything else (e.g. "make the neon
sign read exactly 'Batiyao'", "give her five fingers on the raised hand"), or "" if it needs no
edit or cannot be saved by an edit (wrong person, collage). Answer with JSON only."""

JUDGE_SCHEMA = {"type": "object", "properties": {"pictures": {"type": "array", "items": {
    "type": "object", "properties": {"n": {"type": "integer"}, "score": {"type": "number"},
                                     "fails": {"type": "array", "items": {"type": "string"}},
                                     "fix": {"type": "string"}},
    "required": ["n", "score"]}}, "best": {"type": "integer"}}, "required": ["pictures", "best"]}

PASS_SCORE = 6
FIXABLE_SCORE = 4          # below this a picture is redrawn rather than edited


def _jl(text, default):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def _cast(pid: str, shot) -> list:
    from app import editor
    out = []
    for rid in editor._shot_ref_ids(shot):
        r = db.one("SELECT * FROM prod_refs WHERE id=? AND project_id=?", (rid, pid))
        if r:
            out.append(r)
    return out


def _brief(pid: str, shot) -> str:
    """Everything the planner needs to plan the picture."""
    from app import editor
    project = db.one("SELECT * FROM projects WHERE id=?", (pid,))
    scene = db.one("SELECT * FROM prod_scenes WHERE id=?", (shot["scene_id"],)) \
        if shot["scene_id"] else None
    shots = editor.ordered_shots(pid)
    i = [s["id"] for s in shots].index(shot["id"]) if shot["id"] in [s["id"] for s in shots] else 0
    prev = shots[i - 1] if i > 0 else None
    rows = _cast(pid, shot)
    names = {r["id"]: r for r in rows}

    def entry(r) -> str:
        line = f"- {r['name']} ({r['kind']}): {(r['description'] or '').strip()[:700]}"
        goes = [names[x] for x in editor.props_for(pid, shot, r["id"]) if x in names
                and names[x]["kind"] in ("prop", "style")] if r["kind"] in ("character", "location") else []
        if goes:
            line += " | in this shot with: " + "; ".join(
                f"{g['name']} ({g['kind']}): {(g['description'] or '').strip()[:200]}" for g in goes)
        return line
    cast = "\n".join(entry(r) for r in rows) or "- (none listed)"
    params = db.params_of(project)
    return (f"Story: {(project['story'] or '').strip()[:1500]}\n"
            f"Scene: {scene['title'] + ': ' + (scene['summary'] or '') if scene else '-'}\n"
            f"Previous shot: {(prev['title'] + ': ' + (prev['prompt'] or '')[:300]) if prev else '(none)'}\n"
            f"THIS shot: \"{shot['title']}\", framing {shot['framing'] or 'not set'} "
            f"({FRAMING.get(shot['framing'] or '', 'choose what tells the moment best')}).\n"
            f"What happens in it: {(shot['prompt'] or '').strip()}\n"
            f"Picture shape: {params.get('aspect', '9:16')}.\n"
            f"Cast you may use:\n{cast}")


def _key(pid: str, shot) -> str:
    raw = json.dumps([_brief(pid, shot), cfg.PLANNER_MODEL], default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def cached(pid: str, shot) -> dict | None:
    p = _jl(shot["opts"], {}).get("_still") or {}
    return p if p and p.get("key") == _key(pid, shot) else None


async def _ask(messages, schema, num_predict=1500) -> dict:
    from app import agent
    res = await agent._ollama("/api/chat", {
        "model": cfg.PLANNER_MODEL, "stream": False, "think": False, "format": schema,
        "messages": messages,
        "options": {"num_ctx": 16384, "temperature": 0.3, "num_predict": num_predict}},
        timeout=600)
    return agent._parse_json((res.get("message") or {}).get("content", "")) or {}


async def plan_many(pid: str, shot_ids: list[str], turn=None, since: float | None = None) -> int:
    """Plan the pictures of these shots (those not already planned), in one GPU turn."""
    from app import agent
    todo = []
    for sid in shot_ids:
        s = db.one("SELECT * FROM shots WHERE id=? AND project_id=?", (sid, pid))
        if s is not None and not cached(pid, s) and (s["prompt"] or s["title"]):
            todo.append(s)
    if not todo:
        return 0
    own = turn is None
    turn = turn or agent.GpuTurn(None)
    done = 0
    try:
        if own:
            await turn.acquire()
        for s in todo:
            if since is not None:
                from app import editor
                if editor.stopped_since(pid, since):
                    log.info("%s: picture planning stopped by the person", pid)
                    break
            try:
                out = await _ask([{"role": "system", "content": PLAN_RULES},
                                  {"role": "user", "content": _brief(pid, s)}], PLAN_SCHEMA)
            except Exception as exc:                          # noqa: BLE001
                log.warning("no picture plan for %s: %s", s["id"], exc)
                continue
            if len((out.get("prompt") or "").strip()) < 30:
                continue
            opts = _jl(db.one("SELECT opts FROM shots WHERE id=?", (s["id"],))["opts"], {})
            opts["_still"] = {"key": _key(pid, s), "prompt": out["prompt"].strip(),
                              "visible": [str(v) for v in out.get("visible") or []],
                              "place": out.get("place"),
                              "checks": [str(c) for c in (out.get("checks") or [])][:5]}
            db.run("UPDATE shots SET opts=? WHERE id=?", (json.dumps(opts), s["id"]))
            done += 1
            await turn.renew()
    finally:
        if own:
            await turn.release()
    log.info("%s: %d picture plan(s) written by %s", pid, done, cfg.PLANNER_MODEL)
    return done


def visible_chars(pid: str, shot, plan: dict | None, prompt: str) -> list:
    cast = _cast(pid, shot)
    names = [n.lower() for n in (plan or {}).get("visible") or []]
    if not names:          # no plan (the assistant wrote the prompt): who it names
        names = [r["name"].lower() for r in cast if r["name"].lower() in prompt.lower()]
    return [r for r in cast if r["kind"] == "character" and r["name"].lower() in names]


def identity_sources(user, pid: str, shot, plan: dict | None, prompt: str,
                     first: tuple[str, bytes] | None = None,
                     first_says: str = "") -> tuple[list, str]:
    """For the identity model: [first?] + one cast card per visible character (face close-up
    and whole look side by side) + the place if there is room. At most IDENTITY_MAX_SOURCES."""
    from app import editor
    chars = visible_chars(pid, shot, plan, prompt)
    srcs, lines = ([first], [f"Picture 1 {first_says}"]) if first else ([], [])
    if len(chars) == 1 and len(srcs) + 2 <= IDENTITY_MAX_SOURCES:
        # One person: their face close-up and whole look as two separate pictures. A combined
        # card was copied as a split-panel layout in a close-up (2026-11-26 test).
        r = chars[0]
        face = next((i for label, i in editor._identity_pictures(r) if "face" in label), None)
        for i, what in ((face, "face close-up"), (r["image_id"], "whole look")):
            if i and editor._img_done(i):
                try:
                    srcs.append(images.source_from_image(user, i))
                    lines.append(f"Picture {len(srcs)} is {r['name']}'s {what}")
                except Exception:                              # noqa: BLE001
                    pass
        lines.append(f"{r['name']} has exactly that face, hair and clothes -- the same person, "
                     "not a look-alike (not those pictures' pose or background)")
        chars_left = []
    else:
        chars_left = chars
    for r in chars_left:
        if len(srcs) >= IDENTITY_MAX_SOURCES:
            break
        card = cast_card(user, r)
        if card is None:
            continue
        srcs.append(card)
        dressed = any((db.one("SELECT kind FROM prod_refs WHERE id=?", (x,)) or {"kind": ""})["kind"] == "prop"
                      for x in editor.props_for(pid, shot, r["id"]))
        lines.append(f"Picture {len(srcs)} shows {r['name']}: their face close-up and their whole "
                     f"look. {r['name']} has exactly that face and hair"
                     f"{'' if dressed else ' and those clothes'} -- the same person, not a "
                     "look-alike (not that picture's pose or background)")
    for r in chars:
        for x in editor.props_for(pid, shot, r["id"]):
            g = next((c for c in _cast(pid, shot) if c["id"] == x and c["kind"] == "prop"), None)
            if g is not None:
                lines.append(f"{r['name']} wears/has {g['name']}: {(g['description'] or '').strip()[:160]}")
    place_name = ((plan or {}).get("place") or "").lower()
    # The place too whenever there is room (a close-up given no place invented a garbled neon
    # sign behind the host; 2026-11-26).
    if not first and len(srcs) < IDENTITY_MAX_SOURCES:
        for r in _cast(pid, shot):
            if r["kind"] == "location" and r["image_id"] and editor._img_done(r["image_id"]) \
                    and (not place_name or r["name"].lower() == place_name
                         or r["name"].lower() in prompt.lower()):
                try:
                    srcs.append(images.source_from_image(user, r["image_id"]))
                    lines.append(f"Picture {len(srcs)} shows {r['name']} (the room, materials, "
                                 "colours, light) -- not the camera position: compose the frame "
                                 "as described")
                except Exception:                              # noqa: BLE001
                    pass
                break
    styles = [c for c in _cast(pid, shot) if c["kind"] == "style" and (c["description"] or "").strip()]
    if styles:
        lines.append("Style: " + "; ".join((c["description"] or "").strip()[:200] for c in styles[:2]))
    lines.append("Make ONE single photograph of the scene -- not a collage, split screen, panels "
                 "or a reference sheet")
    return srcs, ". ".join(lines) + ("." if lines else "")


def sources_for(user, pid: str, shot, plan: dict | None, prompt: str) -> tuple[list, str]:
    """Source pictures and the sentence naming them: only the characters visible in the
    frame (face close-up first), the place only for wide framings, labelled as a look."""
    from app import editor
    cast = _cast(pid, shot)
    chars = visible_chars(pid, shot, plan, prompt)
    srcs, lines = [], []
    per = 2 if len(chars) == 1 else 1
    close = (shot["framing"] or "") in ("CU", "ECU", "MCU")
    for r in chars[:3]:
        nums = []
        pics = editor._identity_pictures(r)
        if not close and r["image_id"] and editor._img_done(r["image_id"]):
            # Wider than a close-up: the full-body picture first, so the OUTFIT is a source too
            # (with two people each got only the face close-up, and clothes drifted).
            pics = [(f"a photo of {r['name']}", r["image_id"])] + \
                   [p for p in pics if p[1] != r["image_id"]]
        for _, i in pics[:per]:
            if len(srcs) >= images.MAX_SOURCES:
                break
            try:
                srcs.append(images.source_from_image(user, i))
                nums.append(str(len(srcs)))
            except Exception:                                  # noqa: BLE001
                pass
        if nums:
            # With an outfit of their own in this shot, their picture gives only the face and
            # hair (2026-11-25: a waistcoat in the character picture vs "wears a navy T-shirt").
            dressed = any((db.one("SELECT kind FROM prod_refs WHERE id=?", (x,)) or {"kind": ""})["kind"] == "prop"
                          for x in editor.props_for(pid, shot, r["id"]))
            lines.append(f"{r['name']} has exactly the face{', hair' if dressed else ', hair and clothes'} of "
                         f"picture{'s' if len(nums) > 1 else ''} {' and '.join(nums)} (only the person"
                         f"{', NOT those clothes' if dressed else ''}, not that picture's pose or background)")
    # What visible characters always have (a costume, an object): its picture if there is room,
    # its description always.
    for r in chars[:3]:
        for x in editor.props_for(pid, shot, r["id"]):
            g = next((c for c in cast if c["id"] == x and c["kind"] == "prop"), None)
            if g is None:
                continue
            said = f"{r['name']} wears/has {g['name']}: {(g['description'] or '').strip()[:160]}"
            if g["image_id"] and editor._img_done(g["image_id"]) and len(srcs) < images.MAX_SOURCES - 1:
                try:
                    srcs.append(images.source_from_image(user, g["image_id"]))
                    said += f" (exactly as in picture {len(srcs)})"
                except Exception:                              # noqa: BLE001
                    pass
            lines.append(said)
    styles = [c for c in cast if c["kind"] == "style" and (c["description"] or "").strip()]
    if styles:
        lines.append("Style: " + "; ".join((c["description"] or "").strip()[:200] for c in styles[:2]))
    place_name = ((plan or {}).get("place") or "").lower()
    if (shot["framing"] or "") in WIDE and len(srcs) < images.MAX_SOURCES:
        for r in cast:
            if r["kind"] == "location" and r["image_id"] and editor._img_done(r["image_id"]) \
                    and (not place_name or r["name"].lower() == place_name
                         or r["name"].lower() in prompt.lower()):
                try:
                    srcs.append(images.source_from_image(user, r["image_id"]))
                    lines.append(f"picture {len(srcs)} shows what {r['name']} looks like "
                                 "(materials, colours, light) -- not the camera position: "
                                 "compose the frame as described")
                except Exception:                              # noqa: BLE001
                    pass
                break
    return srcs, ". ".join(lines) + ("." if lines else "")


OPTION_MODELS = ["flux-2-klein-9b", "qwen-image-2.1"]

# People shots are drawn with the model made to keep a person's identity (2026-11-26: in the
# podcast's two-shots the outfits matched but the faces drifted in every shot; each person had
# been given ONE source -- the full-body picture -- and klein / Qwen-Image drew a generic face).
IDENTITY_MODEL = "qwen-image-edit-2511"
IDENTITY_MAX_SOURCES = 3            # what Qwen-Image-Edit takes
IDENTITY_STEPS = 20                 # a new frame (WIS default 40: 215 s; 20: ~100 s, same look)
IDENTITY_STEPS_DERIVED = 12         # an edit of a set-up's master frame (~60 s)


def cast_card(user, r) -> tuple[str, bytes] | None:
    """One source picture per character with BOTH their face close-up and their full look side
    by side, so a shot with two or three people still gives the model each face and outfit."""
    from app import editor
    from PIL import Image
    import io
    face = next((i for label, i in editor._identity_pictures(r) if "face" in label), None)
    body = r["image_id"] if r["image_id"] and editor._img_done(r["image_id"]) else None
    ids = [i for i in (face, body) if i]
    if not ids:
        return None
    key = hashlib.sha1("|".join([r["id"]] + ids).encode()).hexdigest()[:16]
    out = cfg.DATA_DIR / "cards" / f"{key}.png"
    if not out.exists():
        tiles = []
        for i in ids:
            row = db.one("SELECT path FROM images WHERE id=?", (i,))
            if not row or not row["path"] or not Path(row["path"]).exists():
                continue
            with Image.open(row["path"]) as im:
                im = im.convert("RGB")
                h = 768
                tiles.append(im.resize((max(1, round(im.width * h / im.height)), h)))
        if not tiles:
            return None
        gap = 16
        card = Image.new("RGB", (sum(t.width for t in tiles) + gap * (len(tiles) - 1), 768), (128, 128, 128))
        x = 0
        for t in tiles:
            card.paste(t, (x, 0))
            x += t.width + gap
        out.parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        card.save(buf, "PNG")
        out.write_bytes(buf.getvalue())
    return f"card_{r['id']}.png", out.read_bytes()


def option_model(i: int, model: str | None) -> str | None:
    """Different models for different options, so the person has a real choice."""
    return model or OPTION_MODELS[i % len(OPTION_MODELS)]


def seed() -> int:
    return random.randint(1, 2 ** 31)


def expected_text(pid: str, shot, plan: dict | None) -> list[str]:
    """Words that should be readable in the frame: quoted text in the plan, the shot and its
    place's description (e.g. a sign reading 'Batiyao')."""
    import re as _re
    bits = [(plan or {}).get("prompt") or "", shot["prompt"] or ""]
    bits += [r["description"] or "" for r in _cast(pid, shot) if r["kind"] in ("location", "prop")]
    found = []
    for b in bits:
        for m in _re.findall(r"(?:reading|reads|says|saying|spelling|text|written|logo)\s*[:\-]?\s*['\"“‘]([^'\"”’]{2,40})['\"”’]", b, _re.I):
            if m.strip() and m.strip() not in found:
                found.append(m.strip())
    return found[:4]


async def judge(pid: str, shot, image_ids: list[str], turn=None) -> dict | None:
    """The planner LOOKS at the finished options against the plan's checks.
    {"best": image_id, "scores": {image_id: score}, "fails": {image_id: [...]}}"""
    from app import agent
    rows = [db.one("SELECT id, path, status FROM images WHERE id=?", (i,)) for i in image_ids]
    rows = [r for r in rows if r and r["status"] == "succeeded" and r["path"]
            and Path(r["path"]).exists()]
    if not rows:
        return None
    pics = []
    for r in rows:
        try:
            from PIL import Image
            import io
            with Image.open(r["path"]) as im:
                im = im.convert("RGB")
                im.thumbnail((768, 768))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=85)
                pics.append(base64.b64encode(buf.getvalue()).decode())
        except Exception:                                      # noqa: BLE001
            pics.append(base64.b64encode(Path(r["path"]).read_bytes()).decode())
    own = turn is None
    turn = turn or agent.GpuTurn(None)
    try:
        if own:
            await turn.acquire()
        if cached(pid, shot) is None:
            # A plan out of date (the shot changed) judged the wrong moment on 2026-11-25:
            # plan again first, in the same turn.
            await plan_many(pid, [shot["id"]], turn)
            shot = db.one("SELECT * FROM shots WHERE id=?", (shot["id"],))
        plan = cached(pid, shot) or {}
        checks = list(plan.get("checks") or [
            f"framing: {FRAMING.get(shot['framing'] or '', 'as the shot needs')}",
            f"it shows the moment the shot starts: {(shot['prompt'] or shot['title'])[:200]}"])
        words = expected_text(pid, shot, plan)
        checks.append("READ EVERY LETTER of any visible text: it must be real, correctly spelled "
                      "words that belong in the scene" +
                      (f" -- here {', '.join(repr(w) for w in words)} (a sign may be partly hidden "
                       "behind someone, but what shows must be those letters)" if words else "") +
                      "; gibberish, garbled or misspelt letters FAIL and need a fix")
        # the characters' cards, after the candidates, so identity can be checked
        cards, names = [], []
        owner = db.one("SELECT user_id FROM projects WHERE id=?", (pid,))
        user = agent.load_user(owner["user_id"]) if owner else None
        for r in visible_chars(pid, shot, plan, plan.get("prompt") or shot["prompt"] or "") if user else []:
            card = cast_card(user, r)
            if card:
                from PIL import Image
                import io
                with Image.open(io.BytesIO(card[1])) as im:
                    im = im.convert("RGB")
                    im.thumbnail((768, 768))
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=85)
                cards.append(base64.b64encode(buf.getvalue()).decode())
                names.append(r["name"])
        from app import editor as _ed
        master = _ed.setup_master(pid, shot, plan, plan.get("prompt") or shot["prompt"] or "")
        if master:
            from PIL import Image
            import io
            with Image.open(master["path"]) as im:
                im = im.convert("RGB")
                im.thumbnail((768, 768))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=85)
            cards.append(base64.b64encode(buf.getvalue()).decode())
            names.append("the MASTER frame of this camera set-up")
        refs = (f"\nAfter them come {len(cards)} reference picture(s): " +
                ", ".join(f"picture {len(rows) + i + 1} = {n}" for i, n in enumerate(names)) +
                ". Check each person against their card, and the room against the master.") if cards else ""
        out = await _ask([{"role": "system", "content": JUDGE_RULES},
                          {"role": "user", "images": pics + cards, "content":
                           f"Shot: \"{shot['title']}\" ({shot['framing'] or 'framing not set'}). "
                           f"The first frame should show: {(plan.get('prompt') or shot['prompt'] or '')[:500]}\nChecks:\n" +
                           "\n".join(f"- {c}" for c in checks) +
                           f"\n\nThere are {len(rows)} candidate pictures, numbered 1-{len(rows)} in order." + refs}],
                         JUDGE_SCHEMA, num_predict=800)
    except Exception as exc:                                   # noqa: BLE001
        log.warning("judging pictures for %s: %s", shot["id"], exc)
        return None
    finally:
        if own:
            await turn.release()
    scores, fails, fixes = {}, {}, {}
    for p in out.get("pictures") or []:
        n = int(p.get("n") or 0)
        if 1 <= n <= len(rows):
            scores[rows[n - 1]["id"]] = float(p.get("score") or 0)
            fails[rows[n - 1]["id"]] = [str(f) for f in (p.get("fails") or [])][:4]
            fixes[rows[n - 1]["id"]] = " ".join(str(p.get("fix") or "").split())[:300]
    if not scores:
        return None
    best = max(scores, key=scores.get)
    return {"best": best, "scores": scores, "fails": fails, "fix": fixes}
