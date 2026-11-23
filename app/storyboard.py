"""Turning a story into shots.

The model animates about five seconds at a time. A story is longer than that,
so "multiple images and a story" becomes: one shot per uploaded image, each
with its own prompt, rendered separately and joined. Splitting the prose into
those beats is the only part a person should not have to do by hand.

The split is a convenience and never a gate. If the host's Ollama is not
configured, is busy, or answers with something unusable, the caller falls back
to a plain even split and the person edits the prompts -- which they can do in
any case, because an auto-written prompt they cannot correct is worse than no
auto-writing at all.
"""
from __future__ import annotations

import logging
import re

from app import wis

log = logging.getLogger("studio.storyboard")

_PLAN_PROMPT = """You are a storyboard director preparing shots for an image-to-video model.

Split the STORY below into exactly {n} consecutive shots, one per supplied image.
Each shot is about {seconds} seconds of screen time.

Rules for each shot's "prompt":
- Describe MOTION and CAMERA, not a still picture. The model already has the
  image; it needs to know what moves and how the camera moves.
- One or two sentences. Concrete and physical. No shot numbers, no "Shot 1:".
- Do not describe a cut, a fade or a transition; each shot is rendered alone.
- Keep the subject continuous with the neighbouring shots.
- English only.

Return STRICT JSON, no prose and no markdown fence:
{{"shots": [{{"title": "three or four words", "prompt": "..."}}]}}

STORY:
{story}
"""


def _fallback(story: str, n: int) -> list[dict]:
    """An even split of the prose, used when the planner is unavailable.

    Sentence-aligned rather than character-aligned, so a shot prompt does not
    begin mid-clause. With fewer sentences than shots the tail repeats the whole
    story, which is honest: the person can see it was not really planned.
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", story.strip()) if s.strip()]
    if not sentences:
        return [{"title": f"Shot {i + 1}", "prompt": ""} for i in range(n)]
    shots, per = [], max(1, len(sentences) / n)
    for i in range(n):
        chunk = sentences[int(i * per):int((i + 1) * per)] or [sentences[min(i, len(sentences) - 1)]]
        shots.append({"title": f"Shot {i + 1}", "prompt": " ".join(chunk)})
    return shots


async def plan(story: str, n: int, seconds: int) -> tuple[list[dict], str]:
    """(shots, how). `how` is 'planned', 'split' or 'no-story' -- shown in the UI.

    The person is told which one they got. A fallback split presented as a
    director's plan is a small lie that costs them a bad render to discover.
    """
    n = max(1, n)
    if not story.strip():
        # NOT "the planner was unavailable" -- the planner was never called.
        # Reporting an empty story as an upstream failure sends the person off
        # to check a service that is working fine, which is exactly what it did
        # on 2026-09-26.
        return _fallback("", n), "no-story"
    try:
        data = await wis.text_json(
            _PLAN_PROMPT.format(n=n, seconds=seconds, story=story.strip()[:6000]))
        raw = data.get("shots")
        shots = []
        if isinstance(raw, list):
            for entry in raw[:n]:
                if isinstance(entry, str):
                    shots.append({"title": "", "prompt": entry.strip()})
                elif isinstance(entry, dict):
                    prompt = str(entry.get("prompt") or entry.get("description") or "").strip()
                    if prompt:
                        shots.append({"title": str(entry.get("title") or "").strip(),
                                      "prompt": prompt})
        if len(shots) == n:
            for i, shot in enumerate(shots):
                shot["title"] = shot["title"] or f"Shot {i + 1}"
            return shots, "planned"
        log.warning("planner returned %d shots, wanted %d -- falling back", len(shots), n)
    except Exception as exc:                       # noqa: BLE001 - never fatal
        log.warning("shot planner unavailable (%s) -- falling back to an even split", exc)
    return _fallback(story, n), "split"


_TAG = re.compile(r"<Picture\s*\d+>", re.I)


def autotag(prompt: str, image_count: int) -> str:
    """Prefix a reference roster onto a single-clip prompt that has no tags.

    MiniMax H3's reference mode resolves references positionally from tags in
    the prompt text, and the service says out loud that an untagged reference
    conditions the model much more weakly. Somebody who attaches six pictures
    and writes prose has not decided to skip the tags -- they have not heard of
    them. So: add a roster line, leave their words alone underneath it, and
    never touch a prompt that already has tags.
    """
    if image_count <= 0 or _TAG.search(prompt):
        return prompt
    roster = ", ".join(f"<Picture {i + 1}>" for i in range(image_count))
    return (f"Use {roster} as visual references for the characters, "
            f"objects and style described below.\n\n{prompt}")
