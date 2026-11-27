"""Production templates: what the person is making, chosen up front in the New production form.

Each format gives the form its few questions (with defaults) and gives the studio's assistant
the BASICS of that kind of production before it plans anything: structure, how it is shot and
cut, audio, and a camera set-up budget. Grounded in a research pass on how real productions
are made (2026-11-27: editing and cinematography guides, Spotify for Creators, TikTok Ads,
Google ABCD, film-school coverage notes, Cinemetrics shot-length data).

The person's rules behind it (2026-11-27): "templates already defined: a movie, an episode, a
podcast, etc; the user should be asked upfront on choices and you should give the basics to the
local agent to begin with"; "make it intuitive and simple"; and, since characters still drift
from picture to picture, "do keep those learnings": every template draws FEW camera set-ups
and reuses them (fixed cameras; close-ups cut from an approved wide), so a face has fewer
chances to change.
"""
from __future__ import annotations

# Rules for every format (the agent gets these with any template).
UNIVERSAL = """\
ALWAYS, whatever the format:
- Cut on purpose: for emotion first, then story, then rhythm. Never cut to fill time, never for
  a one-word "yeah".
- Follow the speaker: a substantive turn shows the person speaking; quick back-and-forth stays
  on the shot with both people.
- Keep screen direction: in one place, each person stays on the same side of the frame in every
  shot (the 180-degree rule).
- Open a new place wide, move closer as it intensifies, come back wide to reset.
- FEW CAMERA SET-UPS: characters still drift a little between separately drawn pictures, so draw
  each place's set-ups once (a wide/two-shot master, one single per speaking character, at most
  one insert) and reuse them for every scene there. A closer framing of an existing set-up is a
  crop of it, not a new drawing.
- Cost-aware: a talking shot is as long as its lines; a moving shot 4-6 s unless the moment needs
  more; never render coverage the edit won't use; a teaser or recap reuses rendered shots.
- Voices first: lines are voiced, then shots are timed to them. Music sits under speech.
- End deliberately: a call to action, a tag, a cliffhanger or a resolution."""

FORMATS: dict[str, dict] = {
    "podcast": {
        "icon": "🎙", "name": "Podcast", "tagline": "hosts talk a topic", "live": True,
        "aspect": "16:9", "ask": "What should they talk about?",
        "placeholder": "Will AI take away more jobs than it creates?",
        "picks": [
            {"key": "kind", "label": "Kind", "options": [["conversation", "Conversation"],
                                                         ["debate", "Debate"],
                                                         ["interview", "Interview"]],
             "default": "conversation"},
            {"key": "minutes", "label": "Length", "options": [[3, "3 min"], [5, "5 min"],
                                                              [10, "10 min"], [15, "15 min"]],
             "default": 5},
            {"key": "hosts", "label": "Hosts", "options": [["auto", "✨ Pick for me"]],
             "default": "auto", "library": True},
        ],
        "more": ["language"],
        "brief": """\
FORMAT: VIDEO PODCAST, filmed like a real multicam studio show.
Structure: optional teaser (10-20 s of the best moment, reused from rendered shots) -> intro
(hosts and topic) -> segments, one scene each, a title card between them -> summary -> outro.
Cameras (fixed, for the WHOLE episode): ONE two-shot of both hosts at the table (shot opts
camera "2S"), and one medium close-up per host (camera = the host's character id). The close-ups
are cut out of the approved two-shot, so faces, clothes and room never change.
Cutting: each segment opens on the two-shot; a quick back-and-forth stays on the two-shot
(talk2, both voices); a turn of about 6 s or more cuts to the speaker's close-up (longtalk);
come back to the two-shot at least every 50 s. Never one person's long turn on a two-shot of
both, never both people in a close-up.
Performance: calm, restrained podcast body language; hands mostly resting; never script who
speaks first or gestures in a talking shot's prompt -- each voice track moves its own lips.
Audio: dialogue drives everything; a short music sting only under intro, title cards and outro.""",
    },
    "film": {
        "icon": "🎬", "name": "Short film", "tagline": "a story with characters", "live": True,
        "aspect": "16:9", "ask": "What's the story?",
        "placeholder": "A night-shift nurse finds a letter addressed to her, dated tomorrow.",
        "picks": [
            {"key": "genre", "label": "Feel", "options": [["drama", "Drama"], ["thriller", "Thriller"],
                                                          ["comedy", "Comedy"], ["action", "Action"]],
             "default": "drama"},
            {"key": "minutes", "label": "Length", "options": [[1, "1 min"], [3, "3 min"],
                                                              [5, "5 min"], [10, "10 min"]],
             "default": 3},
        ],
        "more": ["language"],
        "brief": """\
FORMAT: SHORT FILM.
Structure: three acts scaled down -- set-up ~25%, confrontation ~50%, resolution ~25%. Each scene
enters late and leaves early.
Coverage per scene: a 3-5 s establishing shot of the place, the wide master, then singles of the
speakers (shot / reverse shot on the same side of the 180-degree line), an insert only for an
object that matters. Closer as tension rises; a close-up only for the emotional peak.
Dialogue: short exchanges on the two-shot (talk2); a line over ~6 s on the speaker's single
(lipsync/longtalk); hold briefly on a listener while a line plays when the reaction matters.
Pace: drama 5-10 s per shot; thriller/action 2-5 s (moving shots, animate).
Set-ups: 3-4 per location (master, a single per speaker, one insert), drawn once and reused in
every scene there; closer framings are crops. No new angle per line.
Audio: dialogue, room tone/ambience, score entering at turning points, sound carrying into the
next scene.""",
    },
    "episode": {
        "icon": "📺", "name": "Episode", "tagline": "part of a series", "live": True,
        "aspect": "16:9", "ask": "What happens in this episode?",
        "placeholder": "Episode 2: the flatmates discover the landlord is moving in.",
        "picks": [
            {"key": "minutes", "label": "Length", "options": [[3, "3 min"], [5, "5 min"],
                                                              [10, "10 min"], [15, "15 min"]],
             "default": 5},
            {"key": "ending", "label": "Ends on", "options": [["cliffhanger", "Cliffhanger"],
                                                              ["closed", "A closed story"]],
             "default": "cliffhanger"},
        ],
        "more": ["language"],
        "brief": """\
FORMAT: SERIES EPISODE.
Structure: optional recap (10-20 s, reused shots only) -> cold open (30-90 s, one scene) -> title
card (5 s) -> two or three acts, each ending on a turn -> tag (15-45 s, a single shot or two).
About 4-8 scenes in 10 minutes, 1-2.5 min each.
Shot like a multi-camera show: 2-4 STANDING SETS, each with its set-ups (master, a single per
regular character, one insert) drawn once and reused in every scene and episode on that set --
the strongest way to keep the cast looking the same. Closer framings are crops.
Dialogue and pace as in a short film; voices must stay the same characters' voices across episodes.
Audio: a short theme sting on the title card and act breaks.""",
    },
    "short": {
        "icon": "📱", "name": "Short", "tagline": "Reels / Shorts, ≤60 s", "live": True,
        "aspect": "9:16", "ask": "What's the idea?",
        "placeholder": "Three things nobody tells you about learning to cook.",
        "picks": [
            {"key": "purpose", "label": "Kind", "options": [["story", "Story"], ["tip", "Tip"],
                                                            ["promo", "Promo"]],
             "default": "story"},
            {"key": "seconds", "label": "Length", "options": [[15, "15 s"], [30, "30 s"],
                                                              [60, "60 s"]],
             "default": 30},
        ],
        "more": ["language", "captions"],
        "brief": """\
FORMAT: SHORT VERTICAL VIDEO (Reels / Shorts / TikTok), 9:16.
Structure: the hook in the first 3 s (lead with the payoff, not the set-up) -> the body -> a clear
call to action or a loop back to the start.
Shots: faces and close framings read on a phone; wides lose detail. Change shot every 1.5-4 s in
the first 10 s, then every 3-6 s. Voice from the first frame; captions burned in.
Set-ups: one or two in total; vary with crops (closer / wider of the same frame) and 4 s inserts.""",
    },
    "explainer": {
        "icon": "🧭", "name": "Explainer", "tagline": "narrated, documentary", "live": True,
        "aspect": "16:9", "ask": "What should it explain?",
        "placeholder": "How a seed becomes a forest.",
        "picks": [
            {"key": "minutes", "label": "Length", "options": [[1, "1 min"], [3, "3 min"],
                                                              [5, "5 min"]],
             "default": 3},
            {"key": "voice", "label": "Told by", "options": [["narrator", "A narrator"],
                                                             ["presenter", "A presenter on camera"]],
             "default": "narrator"},
        ],
        "more": ["language"],
        "brief": """\
FORMAT: EXPLAINER / DOCUMENTARY.
Structure: hook -> problem -> solution -> proof -> one call to action (60 s: ~10/20/25/15/10 s).
Narration at 130-150 words a minute.
The narrator's voice drives the timeline, over moving pictures (animate) of 4-6 s for facts and
6-10 s for emotional beats; pictures that add meaning, not literal illustrations of each word.
Interview bites 8-20 s as a close-up of the speaker (longtalk) from ONE set-up per person, reused.
A presenter: one medium close-up set-up for the whole piece, varied with crops.
Audio: narrator on top, natural sound low, music bed ducked under speech.""",
    },
    "music": {
        "icon": "🎵", "name": "Music video", "tagline": "from a song", "live": True,
        "aspect": "16:9", "ask": "What's the song about, and what should we see?",
        "placeholder": "A monsoon love song: two strangers share an umbrella across the city.",
        "picks": [
            {"key": "style", "label": "Kind", "options": [["performance", "Performance"],
                                                          ["story", "Story"], ["mix", "Both"]],
             "default": "mix"},
        ],
        "more": ["language"],
        "brief": """\
FORMAT: MUSIC VIDEO. The song is the master track; the structure follows it (intro, verses,
choruses, bridge, outro).
Performance on the choruses (the singer lip-syncs, lipsync, from 1-2 set-ups; one render per
chorus, crops on phrase changes every 2-4 bars); story shots (animate, 4-8 s) on the verses, cut
on bar lines -- not on every beat. Return to the performer for the big chorus moments.
Variety from new places and shots without faces, not new angles on the characters (at most ~3
character set-ups per location).""",
    },
    "free": {
        "icon": "✏️", "name": "Free-form", "tagline": "anything else", "live": True,
        "aspect": "9:16", "ask": "What's the story?",
        "placeholder": "The story, in a few lines or a few pages. You can also leave it empty and "
                       "develop it with the assistant.",
        "picks": [], "more": ["title"], "brief": "",
    },
}

ORDER = ["podcast", "film", "episode", "short", "explainer", "music", "free"]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get(key: str | None) -> dict | None:
    return FORMATS.get(key or "")


def brief(key: str | None, answers: dict | None = None) -> str:
    """What the assistant is told about the production's format, with the person's choices."""
    f = get(key)
    if not f or not f["brief"]:
        return ""
    picked = []
    for p in f["picks"]:
        v = (answers or {}).get(p["key"])
        if v not in (None, ""):
            same = lambda a, b: str(a) == str(b) or (_num(a) is not None and _num(a) == _num(b))
            label = next((o[1] for o in p["options"] if same(o[0], v)), str(v))
            picked.append(f"{p['label']}: {label}")
    return (f["brief"] + ("\nThe person chose: " + "; ".join(picked) + "." if picked else "")
            + "\n" + UNIVERSAL)


def for_form() -> list[dict]:
    """The cards and questions of the New production form."""
    return [{"key": k, **{x: FORMATS[k][x] for x in ("icon", "name", "tagline", "live", "aspect",
                                                    "ask", "placeholder", "picks", "more")}}
            for k in ORDER]
