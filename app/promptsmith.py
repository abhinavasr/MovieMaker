"""The local 27B writes each shot's final video prompt (2026-11-24, the person: "use 27b
for better prompting across all use cases").

Before a storyboard render, every shot about to be made gets its prompt rewritten by the
planner model (cfg.PLANNER_MODEL, qwen3.8:27b), with guidance for the model that will make
that shot. All shots of a render are written in ONE GPU turn (the same turn the assistant's
planner takes: studio queue paused, a WIS lease, the model unloaded afterwards), so the
video model then has the whole card. A result is cached on the shot (opts._polish) and
reused while the shot's text, use case, settings and characters are unchanged.

Failure is never fatal: a shot whose rewrite fails keeps the prompt the code builds.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

from app import db
from app.config import cfg

log = logging.getLogger("studio.promptsmith")

COMMON = """You write the prompt for an AI video model that makes ONE shot of a film.
Write what is SEEN (and heard, if asked) in the finished clip, in the present tense, concretely:
people, action, expression, light, camera. Describe the result, never give instructions
("a woman with green hair", not "change her hair to green"). Keep every name and every tag
such as <Picture 1>, <Video 1>, <Subject 1>, [Shot 1] and every timestamp exactly as given.
Never add characters, spoken words, captions or on-screen text that are not in the input.
Describe ONLY the events written in "This shot". The "Context" (story, scene, cast) is background
for looks and setting: NEVER add an action from it -- the scene's later beats belong to the next
shots (2026-11-25: a "walks along and spots her" shot was given the scene's "breaks into a
sprint", and he ran out of frame). Keep the shot's pace: walking stays walking.
Answer with JSON only."""

GUIDE = {
    "animate": "Model: MiniMax H3 image-to-video. The clip STARTS EXACTLY ON the shot's picture, "
               "which the model already has: do not describe the still picture; describe what "
               "MOVES and how the CAMERA moves, as one continuous shot. 40-90 words.",
    "perform": "Model: MiniMax H3 reference-to-video. The people and places come from reference "
               "pictures (their tags are added after your text). Describe the action, the "
               "expressions and the camera as ONE continuous shot with no cuts. 50-110 words.",
    "lipsync": "Model: Wan2.2-S2V. The person in the picture speaks or sings a recorded line. "
               "Describe their expression, gestures, head and body movement while speaking, and a "
               "mostly steady camera. Do not write the words. 30-70 words.",
    # InfiniteTalk applies the text to EVERY moment of the clip; the voice track alone decides
    # when the lips move. A scripted gesture or "speaks first" is therefore acted out the whole
    # clip, silent stretches included (2026-11-27: "the girl is making gestures even if there
    # is no dialogue" -- the prompt said she "gestures with her hands", her partner "listens").
    "longtalk": "Model: InfiniteTalk. The person in the picture talks or sings a long recording; "
                "the recording alone moves their lips, and your text applies to the WHOLE clip. "
                "Describe calm, restrained, natural body language: relaxed posture, hands mostly "
                "resting, small head movements, steady eye line, a static camera. Never write "
                "'animated', 'expressive', 'gestures with their hands' or a sequence of actions. "
                "Do not write the words. 30-60 words.",
    "talk2": "Model: InfiniteTalk, two speakers. Each person's OWN voice track moves their lips, "
             "so WHO talks WHEN comes from the audio, never from your text -- and your text applies "
             "to the WHOLE clip, to both people equally. Describe both people the SAME way: seated, "
             "calm, restrained podcast body language, hands resting, small nods, eye contact with "
             "each other, a static camera. Do NOT say who speaks first or next, do NOT script turns, "
             "reactions or a sequence of actions, and never give one person gestures, 'animated' "
             "expressions or 'listens' (they would do it the whole clip, even while silent). Do "
             "not write the words. 35-70 words.",
    "motion": "Model: Wan Animate 2 (motion transfer). The person in the picture performs the "
              "movements of a reference video. Give `prompt` as two lines exactly: "
              "'Character appearance description: ...' (the person in the picture: face, hair, "
              "clothes) and 'Background description: ...' (the setting), and `motion_prompt`: "
              "one sentence describing the movements in the reference video.",
    "recast": "Model: Viggle Remake (MiniMax-H3, six-section prompt). The input is a draft. "
              "Rewrite it keeping the six section names and their order exactly "
              "(subject_definitions, summary, retention_analysis, detailed_description, "
              "overall_soundscape, non_diegetic_music), every tag and timestamp. Describe each "
              "<Subject N> well enough to tell them apart from everyone else in frame, describe "
              "the target video (not instructions), keep retention_analysis literal about what "
              "is preserved and what changes, and make [Shot 1] in detailed_description a vivid "
              "timed description of the action in the source.",
    "restyle": "Model: Viggle Remake edit (MiniMax-H3, six-section prompt). The input is a draft. "
               "Rewrite it keeping the six section names and their order exactly, every tag and "
               "timestamp. Split the change into one <Subject N> per thing that changes (e.g. "
               "<Subject 1> her hair, <Subject 2> her clothes, <Subject 3> the headphones), each "
               "defined in subject_definitions and given its own retention_analysis line "
               "('changed - ...' / 'removed - ...'), and write everything as a description of "
               "the TARGET video (never 'remove', 'change' or 'make').",
}

VERSION = 3          # bump to rewrite every cached prompt under new rules
SCHEMA = {"type": "object", "properties": {"prompt": {"type": "string"},
                                           "motion_prompt": {"type": "string"}},
          "required": ["prompt"]}
SIX = ("subject_definitions:", "summary:", "retention_analysis:", "detailed_description:",
       "overall_soundscape:", "non_diegetic_music:")


def _jl(text, default):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def draft(pid: str, shot) -> str:
    """The text the shot would be made from without the rewrite."""
    from app import editor
    make = shot["make"] or "animate"
    if make in editor.REMAKE_MAKES:
        return editor.remake_prompt(pid, shot, editor._remake_frames(shot["seconds"]))
    return (shot["prompt"] or "").strip()


def context(pid: str, shot) -> str:
    """Who and where, as background (never events to add)."""
    from app import editor
    if (shot["make"] or "animate") in editor.REMAKE_MAKES:
        return ""
    return editor._shot_context(pid, shot)


def _key(pid: str, shot, text: str) -> str:
    refs = [db.one("SELECT name, description, image_id FROM prod_refs WHERE id=?", (r,))
            for r in _jl(shot["refs"], [])]
    opts = {k: v for k, v in _jl(shot["opts"], {}).items() if not k.startswith("_")}
    raw = json.dumps([VERSION, shot["make"], text, context(pid, shot), opts, [dict(r) for r in refs if r],
                      shot["seconds"], cfg.PLANNER_MODEL], sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def polished(shot, field: str = "prompt") -> str | None:
    """The cached rewrite for this shot, if it is still current (checked at render)."""
    p = _jl(shot["opts"] if "opts" in shot.keys() else "{}", {}).get("_polish") or {}
    return p.get(field) or None


def _ok(make: str, text: str, out: dict) -> bool:
    prompt = (out.get("prompt") or "").strip()
    if len(prompt) < 20:
        return False
    if make in ("recast", "restyle"):
        if [s for s in SIX if s not in prompt] or prompt.index(SIX[0]) > 5:
            return False
        tags = set(re.findall(r"<(?:Video|Picture) \d>", text))
        if not tags <= set(re.findall(r"<(?:Video|Picture) \d>", prompt)):
            return False
    if make == "motion" and "Background description" not in prompt:
        return False
    return True


async def _ask(make: str, title: str, text: str, ctx: str = "") -> dict | None:
    from app import agent
    user = (f"Use case: {make}. {GUIDE.get(make, GUIDE['animate'])}\n"
            f"Shot title: {title or '-'}\n\nThis shot (the ONLY events to describe):\n{text}"
            + (f"\n\nContext (background only -- do not add its events):\n{ctx}" if ctx else ""))
    res = await agent._ollama("/api/chat", {
        "model": cfg.PLANNER_MODEL, "stream": False, "think": False, "format": SCHEMA,
        "messages": [{"role": "system", "content": COMMON}, {"role": "user", "content": user}],
        "options": {"num_ctx": 16384, "temperature": 0.3, "num_predict": 2500}}, timeout=600)
    out = agent._parse_json((res.get("message") or {}).get("content", "")) or {}
    return out if _ok(make, text, out) else None


async def polish_shots(project_id: str, shot_ids: list[str], report=None) -> int:
    """Rewrite the prompts of these shots that need it, in one GPU turn. Returns how many
    were rewritten."""
    from app import agent
    todo = []
    for sid in shot_ids:
        shot = db.one("SELECT * FROM shots WHERE id=? AND project_id=?", (sid, project_id))
        if shot is None:
            continue
        if shot["make"] == "graphic":
            continue                                          # a chart: drawn by code, no prompt
        text = draft(project_id, shot)
        if not text.strip():
            continue
        key = _key(project_id, shot, text)
        if (_jl(shot["opts"], {}).get("_polish") or {}).get("key") == key:
            continue                                          # still current
        todo.append((shot, text, key))
    if not todo:
        return 0
    if report:
        await report(1, f"the local {cfg.PLANNER_MODEL} is writing {len(todo)} prompt(s)")
    turn = agent.GpuTurn(None)
    done = 0
    try:
        await turn.acquire()
        for shot, text, key in todo:
            try:
                out = await _ask(shot["make"] or "animate", shot["title"], text,
                                 context(project_id, shot))
            except Exception as exc:                          # noqa: BLE001
                log.warning("prompt for %s not rewritten: %s", shot["id"], exc)
                out = None
            if not out:
                continue
            opts = _jl(db.one("SELECT opts FROM shots WHERE id=?", (shot["id"],))["opts"], {})
            opts["_polish"] = {"key": key, "prompt": out["prompt"].strip(),
                               **({"motion_prompt": out["motion_prompt"].strip()}
                                  if out.get("motion_prompt") else {})}
            db.run("UPDATE shots SET opts=? WHERE id=?", (json.dumps(opts), shot["id"]))
            done += 1
            await turn.renew()
    except Exception as exc:                                  # noqa: BLE001
        log.warning("prompt rewriting skipped: %s", exc)
    finally:
        await turn.release()
    log.info("%s: %d of %d prompt(s) rewritten by %s", project_id, done, len(todo),
             cfg.PLANNER_MODEL)
    return done


def current(project_id: str, shot) -> dict:
    """The rewrite to use for this shot now: {} unless it matches the shot as it is."""
    p = _jl(shot["opts"] if "opts" in shot.keys() else "{}", {}).get("_polish") or {}
    if not p:
        return {}
    try:
        return p if p.get("key") == _key(project_id, shot, draft(project_id, shot)) else {}
    except Exception:                                          # noqa: BLE001
        return {}
