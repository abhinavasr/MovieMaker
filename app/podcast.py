"""Podcasts: a researched, fact-checked two-host debate, built as a production in the editor.

The person's design (dual-agent-podcast, 2026-11-26), kept tier for tier:
  Tier 4, per host: Information Collection -> Synthesis -> Fact-Checking
  Tier 1, per host: the persona's friction points, grounded in its checked research
  Tier 2: a neutral orchestrator keeps ONLY the overlapping friction points
  Tier 3: the script, segment by segment
What is different here, and why:
  - Research reads the REAL web (app/websearch.py: DuckDuckGo, Wikipedia; public pages only),
    not the model's memory. Every source is a page that was actually read, and every quote is
    checked against that page's text by code: a quote that is not on the page is marked.
  - Fact-checking searches again, per claim, with a neutral query, and reads fresh pages.
    "Verified" without a checked quote from the evidence is downgraded to "Needs Context".
  - The output is not a .md file but a production in the studio's editor (the person: "the
    output of this will not be md file instead will be a feed for our editor"). The hosts are
    characters (the person chooses their look; their voice is designed from the description),
    the segments are scenes, and each exchange is an InfiniteTalk shot -- `talk2` for a
    two-person exchange in a 2S picture, `longtalk` for a long turn in close-up -- with its
    lines under it ("select the correct model type (infinite talk) with other relevant
    settings"). The sources and fact-checks go into the production's story, so the editor's
    assistant knows them when the person asks for changes.
  - Runs are stored after every agent, so a restart resumes where it stopped. The planner
    model runs on the GPU through agent.GpuTurn (one planner at a time; WIS waits). With no
    review gates the whole run is ONE GPU turn (loading the 27B costs ~15 s each time).
  - The approval gates (the person's step mode) are optional: review the research, review
    the debate points. The assistant starts runs from a chat (make_podcast).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from contextlib import suppress

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import auth, db, service, websearch
from app.config import cfg

log = logging.getLogger("studio.podcast")
router = APIRouter()

ACTIVE = ("queued", "researching", "debating", "writing", "building")
GATES = ("review_research", "review_points")
FINAL = ("done", "failed", "cancelled")
REVIEWS = ("research", "points")

LENSES = {
    "left_right": ("Leftist Theory (structural equity, collective agency, democratic governance)",
                   "Rightist Mindset (individual liberty, free enterprise, decentralized order)"),
    "optimist_skeptic": ("Optimist (what could go right, progress, opportunity)",
                         "Skeptic (what could go wrong, evidence gaps, unintended consequences)"),
    "reform_tradition": ("Reformer (change the system, new approaches)",
                         "Traditionalist (what works already, continuity, proven institutions)"),
}
DEFAULT_HOSTS = [
    {"name": "Elena Vance",
     "look": "a woman in her late thirties with shoulder-length dark brown hair and thin "
             "gold-rimmed glasses, wearing a navy blazer over a cream top",
     "voice": "A woman in her late thirties; a warm, articulate, measured alto with a clear "
              "American accent; calm but incisive."},
    {"name": "Marcus Sterling",
     "look": "a man in his mid-forties with short greying hair and a neat beard, wearing a "
             "charcoal crew-neck sweater over a collared shirt",
     "voice": "A man in his mid-forties; a deep, confident baritone with a crisp American "
              "accent; dry wit, unhurried."},
]
STUDIO = ("A warm, modern podcast studio set: two chairs facing each other across a round wooden "
          "table, a studio microphone on a boom arm and headphones at each seat; acoustic foam "
          "panels, bookshelves and a softly glowing neon sign behind; warm key light, shallow "
          "depth of field. The set is empty; the hosts are placed in each shot.")

MAX_TOPIC = 1500
WORDS_PER_MIN = 150
WORDS_PER_S = 2.6               # the editor's own estimate for a line (add_script)
MAX_LINE_WORDS = 110            # ~42 s: an InfiniteTalk shot holds 60 s
PAGE_CHARS = 5000               # per page given to the collectors
CHECK_CHARS = 3500              # per page given to the fact-checker
PAGES_PER_SIDE = 5
BACKGROUND_PAGES = 3
CHECK_PAGES_PER_CLAIM = 2
MAX_PAGES = 40

STEPS = [  # (state key, label, progress when done) -- for the page's tier view
    ("show", "Showrunner: format, central question, tone and hosts from the topic", 2),
    ("queries", "Research plan: search queries for both hosts", 4),
    ("read", "Reading the web", 14),
    ("a_collect", "Tier 4 · Information collection (host A)", 22),
    ("a_synth", "Tier 4 · Synthesis (host A)", 27),
    ("a_check_pages", "Tier 4 · Fact-check searches (host A)", 32),
    ("a_fact", "Tier 4 · Fact-checking (host A)", 36),
    ("a_revise", "Tier 4 · Researcher revises on the fact-checker's feedback (host A)", 39),
    ("a_final", "Tier 4 · Fact-checker re-checks the revised claims (host A)", 41),
    ("a_persona", "Tier 1 · Persona friction points (host A)", 43),
    ("b_collect", "Tier 4 · Information collection (host B)", 50),
    ("b_synth", "Tier 4 · Synthesis (host B)", 55),
    ("b_check_pages", "Tier 4 · Fact-check searches (host B)", 60),
    ("b_fact", "Tier 4 · Fact-checking (host B)", 63),
    ("b_revise", "Tier 4 · Researcher revises on the fact-checker's feedback (host B)", 66),
    ("b_final", "Tier 4 · Fact-checker re-checks the revised claims (host B)", 68),
    ("b_persona", "Tier 1 · Persona friction points (host B)", 70),
    ("orchestrator", "Tier 2 · Orchestrator: overlapping friction points", 75),
    ("script", "Tier 3 · Script, segment by segment", 92),
    ("built", "Editor production: hosts, scenes, InfiniteTalk shots, lines", 100),
]


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- records

def _row(podcast_id: str):
    return db.one("SELECT * FROM podcasts WHERE id=?", (podcast_id,))


def owned(user: auth.User, podcast_id: str):
    row = _row(podcast_id)
    if row is None or row["user_id"] != user.id:
        raise HTTPException(404, "no such podcast")
    return row


def _state(row) -> dict:
    try:
        return json.loads(row["state"] or "{}")
    except ValueError:
        return {}


def _config(row) -> dict:
    try:
        return json.loads(row["config"] or "{}")
    except ValueError:
        return {}


def _save_state(podcast_id: str, state: dict) -> None:
    db.run("UPDATE podcasts SET state=?, updated_at=? WHERE id=?",
           (json.dumps(state), db.now(), podcast_id))


def _set(podcast_id: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = db.now()
    cols = ", ".join(f"{k}=?" for k in fields)
    db.run(f"UPDATE podcasts SET {cols} WHERE id=?", (*fields.values(), podcast_id))


def _log(podcast_id: str, text: str) -> None:
    row = _row(podcast_id)
    if row is None:
        return
    try:
        items = json.loads(row["log"] or "[]")
    except ValueError:
        items = []
    items.append({"t": db.now(), "text": str(text)[:500]})
    db.run("UPDATE podcasts SET log=?, message=?, updated_at=? WHERE id=?",
           (json.dumps(items[-200:]), str(text)[:300], db.now(), podcast_id))


def _check(podcast_id: str) -> None:
    row = _row(podcast_id)
    if row is None or row["status"] == "cancelled":
        raise Cancelled()


def _clean_config(body: dict) -> dict:
    topic = " ".join(str(body.get("topic") or "").split())
    if not topic:
        raise HTTPException(400, "give the topic or question the hosts should debate")
    if len(topic) > MAX_TOPIC:
        raise HTTPException(400, f"the topic is over {MAX_TOPIC} characters")
    preset = str(body.get("lenses") or "left_right")
    lenses = LENSES.get(preset, LENSES["left_right"])
    hosts_in = body.get("hosts") if isinstance(body.get("hosts"), list) else []
    # Sides nobody spelled out are drawn from the topic by the research planner (2026-11-26: an
    # assistant picked "left vs right" for India-vs-US democracy, then "reformer vs traditionalist"
    # for immigration and Indigenous societies). The page's form always sends its sides.
    names_given = any(isinstance(h, dict) and h.get("name") for h in hosts_in)
    auto_sides = not any(isinstance(h, dict) and (h.get("lens") or h.get("view")) for h in hosts_in) \
        and not body.get("sides_given") and not body.get("lenses")
    hosts = []
    for i in range(2):
        h = hosts_in[i] if i < len(hosts_in) and isinstance(hosts_in[i], dict) else {}
        d = DEFAULT_HOSTS[i]
        hosts.append({
            "name": " ".join(str(h.get("name") or d["name"]).split())[:60],
            "lens": " ".join(str(h.get("lens") or h.get("view") or lenses[i]).split())[:300],
            "look": " ".join(str(h.get("look") or d["look"]).split())[:600],
            "voice": " ".join(str(h.get("voice") or d["voice"]).split())[:600],
        })
    if hosts[0]["name"].lower() == hosts[1]["name"].lower():
        raise HTTPException(400, "the two hosts need different names")
    try:
        minutes = float(body.get("minutes") or 8)
    except (TypeError, ValueError):
        raise HTTPException(400, "minutes must be a number")
    review = [r for r in (body.get("review") or []) if r in REVIEWS]
    from app import editor
    aspect = str(body.get("aspect") or "16:9")
    if aspect not in editor.ASPECTS:
        raise HTTPException(400, f"aspect must be one of {', '.join(editor.ASPECTS)}")
    return {
        "topic": topic,
        "title": " ".join(str(body.get("title") or "").split())[:120],
        "hosts": hosts,
        "minutes": max(3.0, min(30.0, minutes)),
        "aspect": aspect,
        "language": " ".join(str(body.get("language") or "English").split())[:30],
        "focus": " ".join(str(body.get("focus") or "").split())[:1500],
        "review": review,
        "auto_sides": bool(auto_sides),
        "names_given": bool(names_given),
    }


def create(user: auth.User, body: dict, thread_id: str | None = None) -> str:
    config = _clean_config(body)
    if not auth.is_unlimited(user.id):
        n = db.one("SELECT COUNT(*) AS n FROM podcasts WHERE user_id=? AND created_at > ? AND "
                   "status NOT IN ('failed','cancelled')", (user.id, db.now() - 86400))["n"]
        if n >= cfg.USER_PODCASTS_PER_DAY:
            raise HTTPException(429, f"that would go over {cfg.USER_PODCASTS_PER_DAY} podcasts in "
                                     "a day, which is the limit for this account")
    pid = db.new_id("pod")
    db.run("INSERT INTO podcasts (id, user_id, title, topic, config, status, message, thread_id, "
           "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
           (pid, user.id, config["title"], config["topic"], json.dumps(config), "queued",
            "waiting to start", thread_id, db.now(), db.now()))
    _log(pid, f"Podcast created: “{config['topic'][:120]}” — {config['hosts'][0]['name']} vs "
              f"{config['hosts'][1]['name']}, about {config['minutes']:g} minutes")
    kick()
    return pid


# --------------------------------------------------------------------------- the model

async def _llm(podcast_id: str, turn, system: str, user: str, schema: dict, label: str,
               think: int = 8000) -> dict:
    from app import agent
    row = _row(podcast_id)
    brief = show_brief(_config(row)) if row is not None and label != "showrunner" else ""
    messages = [{"role": "system", "content": brief + system}, {"role": "user", "content": user}]
    messages, ctx, est = agent._fit_window(messages, think)
    for attempt in range(3):
        _check(podcast_id)
        await turn.renew()
        t0 = time.monotonic()
        res = await agent._ollama("/api/chat", {
            "model": cfg.PLANNER_MODEL, "messages": messages, "think": True, "stream": False,
            "format": schema, "keep_alive": "10m",
            "options": {"num_ctx": ctx, "num_predict": think,
                        "temperature": 0.5 if attempt == 0 else 0.75}}, timeout=1500)
        content = (res.get("message") or {}).get("content") or ""
        out = agent._parse_json(content)
        log.info("podcast %s %s: prompt ~%d tok, window %d, %.1fs, %s tokens, ok=%s",
                 podcast_id, label, est, ctx, time.monotonic() - t0, res.get("eval_count"),
                 isinstance(out, dict))
        if isinstance(out, dict):
            return out
        if res.get("done_reason") == "length" and think < 32000:
            think = min(32000, think * 2)
            messages, ctx, est = agent._fit_window(messages, think)
    raise RuntimeError(f"{label}: the model did not return a usable answer")


def _obj(props: dict, required: list | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props)}


def _arr(items: dict) -> dict:
    return {"type": "array", "items": items}


S = {"type": "string"}
N = {"type": "number"}
STATUSES = ["Verified", "Partially Verified", "Needs Context", "Disputed"]

QUERIES_SCHEMA = _obj({"title": S, "a_queries": _arr(S), "b_queries": _arr(S),
                       "background_queries": _arr(S)})
SIDES_SCHEMA = _obj({"a_side": S, "b_side": S, "a_name": S, "a_look": S, "a_voice": S,
                     "b_name": S, "b_look": S, "b_voice": S})
COLLECT_SCHEMA = _obj({
    "sources": _arr(_obj({"page": S, "title": S, "type": S, "summary": S, "quote": S,
                          "citation": S})),
    "rawDataPoints": _arr(_obj({"point": S, "page": S}))})
SYNTH_SCHEMA = _obj({
    "thesis": S,
    "pillars": _arr(_obj({"id": S, "heading": S, "argument": S, "supportingSourceIds": _arr(S)})),
    "testableClaims": _arr(_obj({"id": S, "statement": S, "sourceId": S, "check_query": S}))})
FACT_SCHEMA = _obj({
    "overallIntegrityScore": N,
    "verdicts": _arr(_obj({"claimId": S, "statement": S,
                           "status": {"type": "string", "enum": STATUSES},
                           "evidenceNote": S,
                           "evidence": _arr(_obj({"page": S, "quote": S})),
                           "confidence": N})),
    "guardrails": _arr(S)})
PERSONA_SCHEMA = _obj({
    "coreStance": S,
    "frictionPoints": _arr(_obj({"id": S, "title": S, "philosophicalRoot": S,
                                 "critiqueOfOpponent": S, "factualAnchor": S, "intensity": N}))})
ORCH_SCHEMA = _obj({
    "synthesisSummary": S,
    "discardedNonOverlaps": _arr(_obj({"origin": {"type": "string", "enum": ["A", "B"]},
                                       "pointTitle": S, "reason": S})),
    "overlappingFrictionPoints": _arr(_obj({
        "id": S, "title": S, "philosophicalVulnerability": S, "aPosition": S, "bPosition": S,
        "factualGround": S, "debateQuestion": S}))})
LINES_SCHEMA = _obj({"lines": _arr(_obj({
    "speaker": {"type": "string", "enum": ["A", "B"]}, "text": S, "delivery": S,
    "cites": _arr(S)}))})


# --------------------------------------------------------------------------- the web

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9%$]+", " ", str(text or "").lower()).strip()


def quote_on_page(quote: str, page_text: str) -> bool:
    """Is this quote really on the page? Word for word, ignoring case and punctuation."""
    q = _norm(quote)
    return len(q) >= 15 and q in _norm(page_text)


async def _search_all(queries: list[str], n: int = 5) -> list[list[dict]]:
    async def one(q):
        try:
            return (await websearch.search(q, n))["results"]
        except Exception as exc:                               # noqa: BLE001
            log.info("search failed for %r: %s", q, exc)
            return []
    return await asyncio.gather(*(one(q) for q in queries))


async def _read_all(urls: list[str], max_chars: int) -> dict[str, dict]:
    sem = asyncio.Semaphore(5)

    async def one(u):
        async with sem:
            try:
                return u, await websearch.read_page(u, max_chars)
            except Exception as exc:                           # noqa: BLE001
                log.info("could not read %s: %s", u, exc)
                return u, None
    out = {}
    for u, page in await asyncio.gather(*(one(u) for u in urls)):
        if page and len(page.get("text") or "") >= 400:
            out[u] = page
    return out


def _skip_url(url: str) -> bool:
    u = url.lower()
    return u.endswith((".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".zip")) or \
        any(h in u for h in ("youtube.com/", "youtu.be/", "facebook.com/", "instagram.com/",
                             "tiktok.com/", "x.com/", "twitter.com/"))


def _pick(result_lists: list[list[dict]], limit: int, taken: set) -> list[str]:
    """Interleave the queries' results (1st of each, then 2nd...) so every query counts."""
    picked = []
    for rank in range(10):
        for results in result_lists:
            if rank < len(results):
                u = results[rank].get("url") or ""
                if u and u not in taken and not _skip_url(u):
                    taken.add(u)
                    picked.append(u)
                    if len(picked) >= limit:
                        return picked
    return picked


def _add_pages(state: dict, pages: dict[str, dict], tag: str) -> list[str]:
    have = state.setdefault("pages", {})
    by_url = {p["url"]: pid for pid, p in have.items()}
    ids = []
    for url, page in pages.items():
        if url in by_url:
            ids.append(by_url[url])
            continue
        if len(have) >= MAX_PAGES:
            break
        pid = f"P{len(have) + 1}"
        have[pid] = {"url": page.get("url") or url, "title": page.get("title") or url,
                     "text": page["text"], "tag": tag}
        by_url[url] = pid
        ids.append(pid)
    return ids


def _pages_block(state: dict, ids: list[str], chars: int) -> str:
    out = []
    for pid in ids:
        p = state["pages"].get(pid)
        if p:
            out.append(f"[{pid}] {p['title']}\nURL: {p['url']}\n{p['text'][:chars]}")
    return "\n\n".join(out) or "(no pages could be read)"


# --------------------------------------------------------------------------- tiers

def _host(config: dict, side: str) -> dict:
    return config["hosts"][0 if side == "a" else 1]


def _other(side: str) -> str:
    return "b" if side == "a" else "a"


FORMATS = {
    "debate": {
        "what": "two hosts argue opposing positions on a genuinely contested question",
        "points": "the overlapping friction points: the exact fault lines where both sides directly "
                  "collide (discard points that talk past each other)",
        "persona": "the sharp points where your position clashes with the other host's",
        "segment": "Clash directly: each challenges the other's weak point, with concrete examples.",
    },
    "conversation": {
        "what": "two co-hosts explore a question together from different perspectives; they build "
                "on, question and gently challenge each other; disagreement only where it is real, "
                "never manufactured",
        "points": "the key questions where the two perspectives meet, differ or illuminate each "
                  "other (discard points only one of them can speak to)",
        "persona": "the points your perspective adds, and where you expect the other host to see "
                   "it differently",
        "segment": "Explore it together: each adds what their perspective sees, asks the other "
                   "real questions, and says plainly where they disagree.",
    },
    "interview": {
        "what": "host A interviews B, an expert; A asks sharp, curious questions, voices the "
                "listener's doubts and pushes back; B answers with evidence and concrete examples",
        "points": "the key questions the interview must answer, each where the expert's view is "
                  "tested by a real doubt or counter-argument",
        "persona": "as host: the doubts and questions a thoughtful listener has; as expert: the "
                   "evidence-backed answers and where the common view is wrong",
        "segment": "A asks and presses; B answers with evidence; A follows up on anything vague.",
    },
}
SHOW_SCHEMA = _obj({
    "format": {"type": "string", "enum": list(FORMATS)}, "why_format": S, "question": S,
    "tone": S, "handle_with_care": _arr(S),
    "a_role": S, "b_role": S, "a_research": S, "b_research": S,
    "a_name": S, "a_look": S, "a_voice": S, "b_name": S, "b_look": S, "b_voice": S})


async def _showrunner(pid, turn, config, state):
    """Tier 0, the showrunner: what kind of episode THIS topic should be, and who is in it.

    2026-11-26, after assistants set up an India-vs-US comparison as left vs right and
    immigration's effect on Indigenous societies as reformer vs traditionalist, the person:
    "help the system with prompts that will allow better mechanism by local llm ... based on
    the topics". Every later agent gets this brief (see _llm)."""
    given = not config.get("auto_sides")
    a, b = config["hosts"]
    out = await _llm(pid, turn,
        "You are the showrunner of a two-host podcast made by AI agents (research, fact-checking, "
        "script, then a video with two people talking). From the topic, decide:\n"
        "1. FORMAT: debate (" + FORMATS["debate"]["what"] + "); conversation ("
        + FORMATS["conversation"]["what"] + "); interview (" + FORMATS["interview"]["what"] + "). "
        "Pick what serves THIS topic: a contested yes/no or X-vs-Y question is a debate; a complex, "
        "sensitive or multi-sided subject is usually a conversation; explaining a field, a history "
        "or a technology is usually an interview.\n"
        "2. QUESTION: the sharp central question of the episode, in one sentence.\n"
        "3. TONE: how it should sound (e.g. lively and witty; serious and respectful; warm and curious).\n"
        "4. HANDLE WITH CARE: what needs care on this topic (affected communities, respectful "
        "terms, stereotypes to avoid, contested numbers); [] if nothing special.\n"
        "5. THE TWO HOSTS: each one's role or side in one sentence (who they are, what they argue or "
        "bring) -- for a debate the two positions that really collide on THIS topic (left vs right only "
        "for political policy questions; X vs Y gets one host per side); what each one's research "
        "should look for; and a fitting name, look (gender, age, appearance, clothes) and voice "
        "(gender, age, accent, manner) -- one woman and one man, with backgrounds that suit the "
        "topic, never stereotypes.\nReturn JSON only.",
        f"Topic, in the person's words: {config['topic']}\n"
        + (f"Focus: {config['focus']}\n" if config["focus"] else "")
        + (f"Length: about {config['minutes']:g} minutes. Language: {config['language']}.\n")
        + (f"The person already chose the hosts; keep them exactly (format debate):\n"
           f"A: {a['name']} — {a['lens']}\nB: {b['name']} — {b['lens']}\n" if given else "")
        + (f"The hosts are {a['name']} and {b['name']} (the person's own characters): give them "
           "roles that fit the topic; their names, looks and voices are already set.\n"
           if config.get("names_given") and not given else ""),
        SHOW_SCHEMA, "showrunner", think=4000)
    fmt = out.get("format") if out.get("format") in FORMATS else "debate"
    if given:
        fmt = "debate"
    clean = lambda v, n: " ".join(str(v or "").split())[:n]
    show = {"format": fmt, "why": clean(out.get("why_format"), 300),
            "question": clean(out.get("question"), 300) or config["topic"][:300],
            "tone": clean(out.get("tone"), 200),
            "care": [clean(x, 200) for x in (out.get("handle_with_care") or [])][:5],
            "research": {"a": clean(out.get("a_research"), 300), "b": clean(out.get("b_research"), 300)}}
    if not given:
        hosts = [dict(h) for h in config["hosts"]]
        for i, k in enumerate(("a", "b")):
            if clean(out.get(f"{k}_role"), 300):
                hosts[i]["lens"] = clean(out.get(f"{k}_role"), 300)
            if config.get("names_given"):
                continue                 # the person's hosts: their names, looks and voices stay
            for f, n in (("name", 60), ("look", 600), ("voice", 600)):
                if clean(out.get(f"{k}_{f}"), n):
                    hosts[i][f] = clean(out.get(f"{k}_{f}"), n)
        if hosts[0]["name"].lower() == hosts[1]["name"].lower():
            hosts = [dict(h, name=d["name"]) for h, d in zip(hosts, DEFAULT_HOSTS)]
        config["hosts"] = hosts
    config["show"] = show
    config["auto_sides"] = False
    db.run("UPDATE podcasts SET config=?, updated_at=? WHERE id=?", (json.dumps(config), db.now(), pid))
    a, b = config["hosts"]
    _log(pid, f"Showrunner: a {fmt} — “{show['question']}” — {show['tone']}. "
              f"{a['name']}: {a['lens']} | {b['name']}: {b['lens']}")
    return show


def _fmt(config: dict) -> dict:
    return FORMATS.get((config.get("show") or {}).get("format") or "debate", FORMATS["debate"])


def show_brief(config: dict) -> str:
    """What every agent is told about the episode (format, question, tone, care, the hosts)."""
    show = config.get("show")
    if not show:
        return ""
    a, b = config["hosts"]
    f = FORMATS.get(show["format"], FORMATS["debate"])
    return ("THE EPISODE (from the showrunner): a " + show["format"] + " -- " + f["what"] + ".\n"
            f"Central question: {show['question']}\nTone: {show['tone']}\n"
            + (f"Handle with care: {'; '.join(show['care'])}\n" if show["care"] else "")
            + f"Host A, {a['name']}: {a['lens']}\nHost B, {b['name']}: {b['lens']}\n\n")


async def _queries(pid, turn, config, state):
    a, b = config["hosts"]
    res = (config.get("show") or {}).get("research") or {}
    out = await _llm(pid, turn,
        "You plan web research for a two-host podcast. Write search-engine queries "
        "(3-8 words each, no quotes or operators) that will find real evidence: data, studies, "
        "official statistics, history, expert analysis. Return JSON only.",
        f"Topic: {config['topic']}\n"
        + (f"Research focus / constraints: {config['focus']}\n" if config["focus"] else "")
        + (f"Host A's research should look for: {res['a']}\n" if res.get("a") else "")
        + (f"Host B's research should look for: {res['b']}\n" if res.get("b") else "")
        + "\nGive 3 queries that find the evidence host A would cite, 3 that find the evidence "
          "host B would cite, and 2 neutral background queries (definitions, key facts, recent "
          "developments). Also a short, catchy episode title (under 70 characters)."
        + (f" Write the title in {config['language']}." if config["language"] != "English" else ""),
        QUERIES_SCHEMA, "queries", think=3000)
    clean = lambda xs: [" ".join(str(x).split())[:120] for x in (xs or []) if str(x).strip()][:4]
    return {"title": str(out.get("title") or "")[:120], "a": clean(out.get("a_queries")),
            "b": clean(out.get("b_queries")), "background": clean(out.get("background_queries"))[:3]}


async def _read_web(pid, config, state):
    q = state["queries"]
    taken: set = set()
    a_res, b_res, bg_res = await asyncio.gather(_search_all(q["a"] or [config["topic"]]),
                                                _search_all(q["b"] or [config["topic"]]),
                                                _search_all(q["background"] or [config["topic"]]))
    # Many sites refuse automated readers (2026-11-26 test: 2 of 7 pages for one host could be
    # read), so take about twice what is needed and keep the first that load.
    plan = {"a": _pick(a_res, PAGES_PER_SIDE * 2 + 2, taken), "b": _pick(b_res, PAGES_PER_SIDE * 2 + 2, taken),
            "background": _pick(bg_res, BACKGROUND_PAGES * 2, taken)}
    _log(pid, f"Searched {len(q['a']) + len(q['b']) + len(q['background'])} queries; trying "
              f"{sum(len(v) for v in plan.values())} pages")
    read = await _read_all([u for v in plan.values() for u in v], PAGE_CHARS + 1000)
    out = {}
    for tag, urls in plan.items():
        got = {u: read[u] for u in urls if u in read}
        keep = dict(list(got.items())[:PAGES_PER_SIDE if tag != "background" else BACKGROUND_PAGES])
        out[tag] = _add_pages(state, keep, tag)
    _log(pid, f"Read {sum(len(v) for v in out.values())} pages "
              f"({len(out['a'])} for A, {len(out['b'])} for B, {len(out['background'])} background)")
    return out


async def _collect(pid, turn, config, state, side):
    h = _host(config, side)
    ids = state["read"].get(side, []) + state["read"].get("background", [])
    out = await _llm(pid, turn,
        f"You are a Tier 4 Information Collection Sub-Agent working for {h['name']} ({h['lens']}).\n"
        "You are given web pages that were actually fetched. Gather the raw evidence relevant to "
        "the topic from this perspective, using ONLY these pages -- never your own memory. Every "
        "source must name the page it comes from (its [P..] id) and carry a SHORT quote (one "
        "sentence, 10-40 words) copied word for word from that page. Prefer data, studies, "
        "official figures and historical precedent over opinion. Return JSON only.",
        f"Topic: {config['topic']}\n"
        + (f"Research focus: {config['focus']}\n" if config["focus"] else "")
        + f"\nPAGES:\n{_pages_block(state, ids, PAGE_CHARS)}\n\n"
          "Gather 4-6 distinct, high-signal sources (type: Empirical Study | Historical Precedent | "
          "Policy Data | Theoretical Text | Expert Analysis | News Report) and 4-6 concrete raw data "
          "points, each with its page id.",
        COLLECT_SCHEMA, f"{side}_collect")
    pages = state["pages"]
    sources = []
    n0 = len(state.get("sources", {}))
    for s in out.get("sources") or []:
        page = str(s.get("page") or "").strip("[] ")
        if page not in pages:
            continue                      # a page that was not read: not a source
        sid = f"S{n0 + len(sources) + 1}"
        quote = str(s.get("quote") or "")[:400]
        sources.append({"id": sid, "side": side, "page": page, "url": pages[page]["url"],
                        "title": str(s.get("title") or pages[page]["title"])[:200],
                        "type": str(s.get("type") or "")[:40],
                        "summary": str(s.get("summary") or "")[:600],
                        "citation": str(s.get("citation") or "")[:200],
                        "quote": quote, "quote_ok": quote_on_page(quote, pages[page]["text"])})
    state.setdefault("sources", {}).update({s["id"]: s for s in sources})
    points = [{"point": str(p.get("point") or "")[:400], "page": str(p.get("page") or "").strip("[] ")}
              for p in out.get("rawDataPoints") or [] if p.get("point")]
    bad = sum(1 for s in sources if not s["quote_ok"])
    _log(pid, f"{h['name']}: {len(sources)} sources from the pages read"
              + (f" ({bad} quote(s) not found word for word on their page)" if bad else ""))
    return {"source_ids": [s["id"] for s in sources], "rawDataPoints": points}


def _sources_block(state: dict, ids: list[str]) -> str:
    out = []
    for sid in ids:
        s = state["sources"].get(sid)
        if s:
            out.append(f"{sid} [{s['page']}] {s['title']} — {s['type']}. {s['summary']} "
                       f"Quote{'' if s['quote_ok'] else ' (NOT found on the page)'}: “{s['quote']}” "
                       f"({s['url']})")
    return "\n".join(out) or "(no sources)"


async def _synth(pid, turn, config, state, side):
    h = _host(config, side)
    c = state[f"{side}_collect"]
    out = await _llm(pid, turn,
        f"You are a Tier 4 Synthesis Sub-Agent working for {h['name']} ({h['lens']}).\n"
        "Compile the collected evidence into coherent thematic pillars and testable claims. Each "
        "claim must be specific and checkable (a number, a causal effect, a historical fact), "
        "and comes with `check_query`: a NEUTRAL search query (3-8 words) an independent "
        "fact-checker would use to verify it -- not phrased to confirm it. Return JSON only.",
        f"Topic: {config['topic']}\n\nSOURCES:\n{_sources_block(state, c['source_ids'])}\n\n"
        f"RAW DATA POINTS:\n" + "\n".join(f"- {p['point']} [{p['page']}]" for p in c["rawDataPoints"])
        + "\n\nCompile 3 pillars and 3-4 testable claims (claim ids claim-1...; sourceId = an S id).",
        SYNTH_SCHEMA, f"{side}_synth")
    claims = []
    for i, cl in enumerate(out.get("testableClaims") or []):
        if cl.get("statement"):
            claims.append({"id": f"{side.upper()}-claim-{i + 1}",
                           "statement": str(cl["statement"])[:500],
                           "sourceId": str(cl.get("sourceId") or ""),
                           "check_query": " ".join(str(cl.get("check_query") or cl["statement"]).split())[:120]})
    return {"thesis": str(out.get("thesis") or "")[:500],
            "pillars": [{"heading": str(p.get("heading") or "")[:160],
                         "argument": str(p.get("argument") or "")[:900],
                         "sources": [str(x) for x in p.get("supportingSourceIds") or []][:6]}
                        for p in (out.get("pillars") or [])[:4]],
            "claims": claims[:5]}


async def _check_pages(pid, config, state, side, claims=None):
    """Independent evidence for each claim: a neutral search, fresh pages."""
    claims = state[f"{side}_synth"]["claims"] if claims is None else claims
    results = await _search_all([c["check_query"] for c in claims], 5)
    taken = set()
    plan = []
    for c, res in zip(claims, results):
        urls = [r["url"] for r in res if r.get("url") and not _skip_url(r["url"])]
        fresh = [u for u in urls if u not in taken][:CHECK_PAGES_PER_CLAIM * 2 + 1]
        taken.update(fresh)
        plan.append((c["id"], fresh))
    read = await _read_all([u for _, us in plan for u in us], CHECK_CHARS + 500)
    out = {}
    for cid, urls in plan:
        got = {u: read[u] for u in urls if u in read}
        out[cid] = _add_pages(state, dict(list(got.items())[:CHECK_PAGES_PER_CLAIM]), "check")
    _log(pid, f"{_host(config, side)['name']}'s fact-checker: read {sum(len(v) for v in out.values())} "
              f"independent pages for {len(claims)} claim(s)")
    return out


def _claim_page(state: dict, claim: dict) -> str | None:
    """The page a claim rests on: its source's page, or a page it names directly."""
    ref = claim.get("sourceId") or ""
    if ref in state.get("sources", {}):
        return state["sources"][ref]["page"]
    return ref if ref in state.get("pages", {}) else None


async def _judge(pid, turn, config, state, side, claims, checks, label) -> dict:
    """The fact-checker's verdicts on `claims`, each against its own evidence pages."""
    h = _host(config, side)
    blocks = []
    for c in claims:
        ids = [x for x in [_claim_page(state, c)] if x] + checks.get(c["id"], [])
        blocks.append(f"CLAIM {c['id']}: {c['statement']}\nEvidence pages for it:\n"
                      + _pages_block(state, list(dict.fromkeys(ids)), CHECK_CHARS))
    out = await _llm(pid, turn,
        f"You are a Tier 4 Fact-Checking Sub-Agent auditing the research of {h['name']} "
        f"({h['lens']}).\nYour mandate is strict factual integrity. Judge every claim ONLY "
        "against its evidence pages (fetched from the web; the independent ones were found "
        "with a neutral search). Do NOT rubber-stamp talking points: flag caveats, missing "
        "context and counter-evidence. Status: Verified (the evidence states it), Partially "
        "Verified, Needs Context, Disputed (the evidence contradicts it or experts disagree). "
        "Every verdict quotes its evidence word for word (one sentence) with the page id. Your "
        "evidenceNote goes back to the researcher: say exactly what is wrong or missing and what "
        "the evidence does support. Return JSON only.",
        f"Topic: {config['topic']}\n\n" + "\n\n".join(blocks)
        + "\n\nGive one verdict per claim (claimId as given) and 2-3 factual guardrails the host "
          "must respect in the debate.",
        FACT_SCHEMA, label, think=10000)
    pages = state["pages"]
    verdicts = []
    for v in out.get("verdicts") or []:
        claim = next((c for c in claims if c["id"] == v.get("claimId")), None)
        if claim is None or any(x["claimId"] == claim["id"] for x in verdicts):
            continue
        ev = []
        for e in v.get("evidence") or []:
            page = str(e.get("page") or "").strip("[] ")
            if page in pages:
                quote = str(e.get("quote") or "")[:400]
                ev.append({"page": page, "url": pages[page]["url"], "title": pages[page]["title"],
                           "quote": quote, "quote_ok": quote_on_page(quote, pages[page]["text"])})
        status = v.get("status") if v.get("status") in STATUSES else "Needs Context"
        note = str(v.get("evidenceNote") or "")[:700]
        if status == "Verified" and not any(e["quote_ok"] for e in ev):
            # Code, not the model, decides what counts as shown: no checked quote, no "Verified".
            status = "Needs Context"
            note = (note + " (Marked Needs Context: no quote from the evidence was found on its "
                           "page.)").strip()
        verdicts.append({"claimId": claim["id"], "statement": claim["statement"], "status": status,
                         "note": note, "evidence": ev,
                         "confidence": max(0.0, min(1.0, float(v.get("confidence") or 0)))})
    for c in claims:                       # a claim the checker skipped is not verified
        if not any(v["claimId"] == c["id"] for v in verdicts):
            verdicts.append({"claimId": c["id"], "statement": c["statement"],
                             "status": "Needs Context", "note": "The fact-checker gave no verdict.",
                             "evidence": [], "confidence": 0.0})
    return {"score": out.get("overallIntegrityScore"), "verdicts": verdicts,
            "guardrails": [str(g)[:400] for g in (out.get("guardrails") or [])][:4]}


def _tally(verdicts: list) -> str:
    t = {s: sum(1 for v in verdicts if v["status"] == s) for s in STATUSES}
    return ", ".join(f"{n} {s.lower()}" for s, n in t.items() if n) or "no verdicts"


async def _fact(pid, turn, config, state, side):
    syn = state[f"{side}_synth"]
    out = await _judge(pid, turn, config, state, side, syn["claims"],
                       state[f"{side}_check_pages"], f"{side}_fact")
    _log(pid, f"{_host(config, side)['name']}'s fact-checker → researcher: {_tally(out['verdicts'])}")
    return out


REVISE_SCHEMA = _obj({
    "claims": _arr(_obj({"id": S, "action": {"type": "string",
                                              "enum": ["keep", "reword", "drop", "replace", "add"]},
                         "statement": S, "sourceId": S, "check_query": S, "why": S})),
    "thesis": S})


async def _revise(pid, turn, config, state, side):
    """The fact-checker's message goes back to the researcher (the person, 2026-11-26: "add 1
    agent each on both the researchers to perform fact check and give back the message to
    the agents"): the researcher keeps, rewords, drops or replaces each claim on the evidence."""
    h = _host(config, side)
    syn, fact = state[f"{side}_synth"], state[f"{side}_fact"]
    ev_pages = sorted({e["page"] for v in fact["verdicts"] for e in v["evidence"]} |
                      {p for ids in state[f"{side}_check_pages"].values() for p in ids})
    feedback = "\n".join(
        f"{v['claimId']} [{v['status']}] {v['statement']}\n  Fact-checker: {v['note']}"
        + "".join(f"\n  Evidence [{e['page']}]: “{e['quote']}”" for e in v["evidence"][:3])
        for v in fact["verdicts"])
    out = await _llm(pid, turn,
        f"You are the Tier 4 Research Agent of {h['name']} ({h['lens']}). Your fact-checker has "
        "audited your claims and sends its verdicts back to you. Revise honestly: KEEP a claim "
        "that is Verified; REWORD one that is Partially Verified or Needs Context so it says only "
        "what the evidence supports (add the caveat); DROP a Disputed claim you cannot support, "
        "or REPLACE it with a different claim the evidence pages DO support (sourceId = the "
        "S id of a source, or the [P..] id of an evidence page). Never keep a claim word for word "
        "if the checker found it wrong. `id` is ALWAYS the id of the claim you act on (keep, "
        "reword, drop and replace all use the original claim's id); only a brand-new extra claim "
        "uses action add with a new id. Each reworded, replaced or added claim gets a NEUTRAL "
        "check_query. Return JSON only.",
        f"Topic: {config['topic']}\nYour thesis: {syn['thesis']}\n\nFACT-CHECKER'S VERDICTS:\n{feedback}"
        f"\n\nGUARDRAILS: " + "; ".join(fact["guardrails"])
        + f"\n\nYOUR SOURCES:\n{_sources_block(state, state[f'{side}_collect']['source_ids'])}"
        + f"\n\nEVIDENCE PAGES:\n{_pages_block(state, ev_pages, 1500)}"
        + "\n\nGive one entry per claim, with that claim's own id, and your thesis, revised if "
          "the verdicts call for it.",
        REVISE_SCHEMA, f"{side}_revise", think=8000)
    old = {c["id"]: c for c in syn["claims"]}
    final, changed, withdrawn, n_new = [], [], [], 0
    for c in out.get("claims") or []:
        act = c.get("action")
        cid = str(c.get("id") or "")
        if act == "keep" and cid in old:
            final.append(old[cid])
        elif act == "drop" and cid in old:
            v = next((x for x in fact["verdicts"] if x["claimId"] == cid), None)
            withdrawn.append({**old[cid], "status": v["status"] if v else "", "why": str(c.get("why") or "")[:300]})
        elif act in ("reword", "replace", "add") and c.get("statement"):
            n_new += 1
            new = {"id": f"{side.upper()}-claim-r{n_new}", "statement": str(c["statement"])[:500],
                   "sourceId": str(c.get("sourceId") or "").strip("[] "),
                   "check_query": " ".join(str(c.get("check_query") or c["statement"]).split())[:120],
                   "revises": cid if cid in old else None, "why": str(c.get("why") or "")[:300]}
            final.append(new)
            changed.append(new)
            if act == "replace" and cid in old:
                withdrawn.append({**old[cid], "status": next((x["status"] for x in fact["verdicts"]
                                                              if x["claimId"] == cid), ""),
                                  "why": new["why"] or "replaced"})
    for cid, c in old.items():
        # A claim the researcher did not mention stays -- unless it was Disputed: one it did
        # not defend is withdrawn (2026-11-26 test: a replacement came back under a new id and
        # the disputed original slipped back in).
        if any(x.get("id") == cid or x.get("revises") == cid for x in final) or \
                any(w["id"] == cid for w in withdrawn):
            continue
        status = next((x["status"] for x in fact["verdicts"] if x["claimId"] == cid), "")
        if status == "Disputed":
            withdrawn.append({**c, "status": status, "why": "not defended after the fact-check"})
        else:
            final.append(c)
    kept = sum(1 for c in final if c["id"] in old)
    _log(pid, f"{h['name']}'s researcher revised after the fact-check: kept {kept}, "
              f"rewrote or replaced {len(changed)}, withdrew {len(withdrawn)}")
    return {"thesis": str(out.get("thesis") or syn["thesis"])[:500], "claims": final,
            "changed": [c["id"] for c in changed], "withdrawn": withdrawn}


async def _recheck(pid, turn, config, state, side):
    """The fact-checker checks the reworded and new claims again, with fresh searches; the
    final verdicts are the kept claims' first verdicts plus these."""
    rev, fact = state[f"{side}_revise"], state[f"{side}_fact"]
    changed = [c for c in rev["claims"] if c["id"] in rev["changed"]]
    verdicts = [v for v in fact["verdicts"] if any(c["id"] == v["claimId"] for c in rev["claims"])]
    guard = list(fact["guardrails"])
    if changed:
        checks = await _check_pages(pid, config, state, side, changed)
        again = await _judge(pid, turn, config, state, side, changed, checks, f"{side}_recheck")
        verdicts += again["verdicts"]
        guard = list(dict.fromkeys(guard + again["guardrails"]))[:5]
        _log(pid, f"{_host(config, side)['name']}'s fact-checker re-checked {len(changed)} revised "
                  f"claim(s): {_tally(again['verdicts'])}")
    return {"score": fact.get("score"), "verdicts": verdicts, "guardrails": guard,
            "withdrawn": rev["withdrawn"], "first_round": fact["verdicts"]}


def _final(state: dict, side: str) -> dict:
    """The fact-check that counts: after the researcher's revision, else the first round."""
    return state.get(f"{side}_final") or state[f"{side}_fact"]


def _research(state: dict, side: str) -> dict:
    syn = state[f"{side}_synth"]
    rev = state.get(f"{side}_revise") or {}
    return {"thesis": rev.get("thesis") or syn["thesis"], "pillars": syn["pillars"],
            "claims": [{k: c[k] for k in ("id", "statement", "sourceId")}
                       for c in (rev.get("claims") or syn["claims"])]}


def _final_brief(state: dict, side: str) -> dict:
    f = _final(state, side)
    return {"verdicts": [{k: v[k] for k in ("claimId", "statement", "status", "note")}
                         for v in f["verdicts"]],
            "withdrawn": [w["statement"] for w in f.get("withdrawn") or []],
            "guardrails": f["guardrails"]}



async def _persona(pid, turn, config, state, side):
    h, o = _host(config, side), _host(config, _other(side))
    out = await _llm(pid, turn,
        f"You are {h['name']}, a Tier 1 Persona Agent embodying {h['lens']}. The other host is "
        f"{o['name']} ({o['lens']}).\nUsing ONLY your validated synthesis and the fact-check "
        f"verdicts and guardrails, extract your core points: {_fmt(config)['persona']} "
        "(philosophical, structural, practical). Anchor each point in a Verified or Partially "
        "Verified fact; never in a Disputed or withdrawn one. Return JSON only.",
        f"Topic: {config['topic']}\n\nSYNTHESIS (revised after the fact-check):\n"
        f"{json.dumps(_research(state, side), ensure_ascii=False)}"
        f"\n\nFACT-CHECK (final):\n{json.dumps(_final_brief(state, side), ensure_ascii=False)}"
        "\n\nExtract 4 sharp, well-grounded points (intensity 1-10).",
        PERSONA_SCHEMA, f"{side}_persona")
    return {"coreStance": str(out.get("coreStance") or "")[:600],
            "frictionPoints": [{k: (str(fp.get(k) or "")[:600] if k != "intensity" else fp.get(k))
                                for k in ("title", "philosophicalRoot", "critiqueOfOpponent",
                                          "factualAnchor", "intensity")}
                               for fp in (out.get("frictionPoints") or [])[:5]]}


async def _orchestrate(pid, turn, config, state):
    a, b = config["hosts"]
    out = await _llm(pid, turn,
        "You are the Tier 2 Orchestrator & Synthesis Engine: strictly non-partisan and analytical. "
        f"Analyse the points produced independently by host A, {a['name']} ({a['lens']}), "
        f"and host B, {b['name']} ({b['lens']}), and isolate ONLY {_fmt(config)['points']}. "
        "Each one carries a shared factual baseline both hosts must acknowledge (from the "
        "fact-checks) and a sharp, neutral question that drives that segment. Order them so the "
        "episode builds. Return JSON only.",
        f"Topic: {config['topic']}\n\nHOST A ({a['name']}):\n{json.dumps(state['a_persona'], ensure_ascii=False)}"
        f"\n\nHOST B ({b['name']}):\n{json.dumps(state['b_persona'], ensure_ascii=False)}"
        "\n\nIsolate 3 to 4 high-impact overlapping friction points and list the discarded ones.",
        ORCH_SCHEMA, "orchestrator")
    points = [{k: str(p.get(k) or "")[:700] for k in ("title", "philosophicalVulnerability",
                                                       "aPosition", "bPosition", "factualGround",
                                                       "debateQuestion")}
              for p in (out.get("overlappingFrictionPoints") or [])[:5] if p.get("title")]
    if not points:
        raise RuntimeError("the orchestrator found no overlapping friction points")
    return {"summary": str(out.get("synthesisSummary") or "")[:800], "points": points,
            "discarded": [{k: str(d.get(k) or "")[:300] for k in ("origin", "pointTitle", "reason")}
                          for d in (out.get("discardedNonOverlaps") or [])[:8]]}


# --------------------------------------------------------------------------- tier 3: the script

def _segments(config: dict, points: list[dict]) -> list[dict]:
    total = config["minutes"] * WORDS_PER_MIN
    open_w, close_w = max(90, total * 0.08), max(110, total * 0.10)
    each = max(160, (total - open_w - close_w) / max(1, len(points)))
    segs = [{"key": "opening", "title": "Opening", "words": int(open_w)}]
    for i, p in enumerate(points):
        segs.append({"key": f"point{i + 1}", "title": p["title"], "point": p, "words": int(each)})
    segs.append({"key": "closing", "title": "Closing", "words": int(close_w)})
    return segs


def _facts_block(state: dict) -> str:
    out = []
    for side in ("a", "b"):
        f = _final(state, side)
        for v in f["verdicts"]:
            out.append(f"- [{v['status']}] {v['statement']} — {v['note']}")
        for w in f.get("withdrawn") or []:
            out.append(f"- [WITHDRAWN after fact-check: do not state as fact] {w['statement']}")
        for g in f["guardrails"]:
            out.append(f"- GUARDRAIL: {g}")
    return "\n".join(out)


async def _write_segment(pid, turn, config, state, seg, prev_lines):
    a, b = config["hosts"]
    lang = config["language"]
    first = "B" if prev_lines and prev_lines[-1]["speaker"] == "A" else "A"
    fmt = (config.get("show") or {}).get("format") or "debate"
    brief = {"opening": f"Open the episode. {a['name']} (A) welcomes the listeners, names the "
                        f"episode and its central question, introduces {b['name']} (B)"
                        + (" as the guest and why they are the right person to ask" if fmt == "interview"
                           else "") + "; B answers and says what is at stake for them. Frame it "
                        "in plain words.",
             "closing": ("Close the episode: A sums up what the listener learned and the question "
                         "that stays open; B gives a last thought; A thanks B and the listeners and "
                         "signs off." if fmt == "interview" else
                         "Close the episode: each host gives a final reflection -- what they now "
                         "grant the other, what they still hold -- and A thanks the listeners and "
                         "signs off.")}.get(seg["key"])
    if brief is None:
        p = seg["point"]
        brief = (f"Segment: {p['title']}\nThe tension: {p['philosophicalVulnerability']}\n"
                 f"A's position: {p['aPosition']}\nB's position: {p['bPosition']}\n"
                 f"Shared factual ground both must acknowledge: {p['factualGround']}\n"
                 f"The question driving it: {p['debateQuestion']}\n"
                 + _fmt(config)["segment"])
    sources = "\n".join(f"{s['id']}: {s['title']} ({s['url']})" + ("" if s["quote_ok"] else " [quote unchecked]")
                        for s in state["sources"].values())
    # 2026-11-26 test: "about 180 words" gave 2 long speeches, not a debate. Ask for the turns.
    turns = max(3 if seg["key"] in ("opening", "closing") else 4, round(seg["words"] / 45))
    order = ", ".join(("A" if (i % 2 == 0) == (first == "A") else "B") for i in range(turns))
    writer_rules = (
        f"You are the Tier 3 Execution & Scripting Engine of a two-host podcast ({fmt}). Write the "
        f"lines of ONE segment. Only two speakers: A = {a['name']} ({a['lens']}) and "
        f"B = {b['name']} ({b['lens']}); no moderator. Keep to the episode's tone.\n"
        "THESE LINES ARE SPOKEN ALOUD by a voice model and lip-synced on video, so:\n"
        "- `text` is exactly what is said: no stage directions, no markdown, no brackets, no "
        "source ids, no URLs; numbers, dates and abbreviations written the way they are said.\n"
        f"- 15 to {MAX_LINE_WORDS} words per line; mix quick exchanges with fuller arguments; "
        "speakers alternate (A, B, A, B...).\n"
        "- `delivery`: a short performance cue (e.g. \"leaning in, measured\", \"amused, quick\").\n"
        "- `cites`: the source ids (S..) behind any fact in the line; [] if none.\n"
        "- Facts come ONLY from the fact-checks and sources given. A Disputed claim may only be "
        "raised as contested, and the other host challenges it; a withdrawn claim is never stated. "
        "Respect every guardrail and everything marked handle-with-care.\n"
        "- Natural, sharp, intellectually honest; hosts use each other's first names now and then."
        + (f"\n- Write the lines in {lang}." if lang != "English" else "")
        + " Return JSON only.")
    context = (f"FACT-CHECKS AND GUARDRAILS:\n{_facts_block(state)}\n\nSOURCES:\n{sources}\n\n"
               + ("THE LINES JUST BEFORE THIS SEGMENT:\n"
                  + "\n".join(f"{l['speaker']}: {l['text']}" for l in prev_lines[-4:]) if prev_lines
                  else "This is the first segment."))

    async def draft(notes: str = "", previous: list | None = None) -> list[dict]:
        best = []
        for attempt in range(2):
            out = await _llm(pid, turn, writer_rules,
                f"Episode: {state.get('title') or config['topic']}\nTopic: {config['topic']}\n\n{brief}\n\n"
                f"Write EXACTLY {turns} lines, in this speaker order: {order}. About {seg['words']} "
                f"words in total.\n\n"
                + ("" if attempt == 0 else f"Your last answer had only {len(best)} lines: this time "
                   f"{turns} separate lines, each a separate turn.\n\n")
                + (("YOUR DRAFT:\n" + "\n".join(f"{i + 1}. {l['speaker']}: {l['text']}"
                                                 for i, l in enumerate(previous or [])) +
                    f"\n\nTHE SCRIPT EDITOR'S NOTES -- fix every one, keep what works:\n{notes}\n\n")
                   if notes else "")
                + context,
                LINES_SCHEMA, f"script {seg['key']}" + (" rewrite" if notes else ""), think=10000)
            lines = _clean_lines(out, state)
            if len(lines) > len(best):
                best = lines
            if len(best) >= max(3, turns // 2 + 1):
                break
        return best

    lines = await draft()
    if len(lines) < 2:
        raise RuntimeError(f"the script for “{seg['title']}” came back empty")
    # The script editor reads the draft and sends notes back to the writer (the same loop as
    # fact-checker -> researcher): mechanical problems found by code, the rest by the model.
    problems = _mechanical_problems(lines, turns, state)
    review = await _llm(pid, turn,
        "You are the script editor of this podcast. Review ONE segment's draft and list what must "
        "change, line by line: off the segment's question; a host speaking against their own role "
        "or side; a fact stated that is not in the fact-checks, or a Disputed claim stated as "
        "settled, or a withdrawn claim used; anything careless about what must be handled with "
        "care; wrong tone; repeating the lines before; stiff, written-sounding speech; a host "
        "talking past the other instead of answering. Be strict but list only real problems; "
        "ok=true when none. Return JSON only.",
        f"{brief}\n\nDRAFT:\n" + "\n".join(f"{i + 1}. {l['speaker']}: {l['text']}" for i, l in enumerate(lines))
        + f"\n\n{context}",
        REVIEW_SCHEMA, f"review {seg['key']}", think=5000)
    problems += [f"line {p.get('line')}: {p.get('problem')}" for p in review.get("problems") or []
                 if str(p.get("problem") or "").strip()]
    if not problems:
        return lines
    notes = "\n".join(f"- {x}" for x in problems[:12])
    _log(pid, f"Script editor → writer on “{seg['title'][:60]}”: {len(problems)} note(s)")
    again = await draft(notes, lines)
    if len(again) >= max(2, int(len(lines) * 0.6)) and \
            len(_mechanical_problems(again, turns, state)) <= len(_mechanical_problems(lines, turns, state)):
        return again
    return lines


REVIEW_SCHEMA = _obj({"ok": {"type": "boolean"},
                      "problems": _arr(_obj({"line": {"type": "integer"}, "problem": S}))})


def _mechanical_problems(lines: list[dict], turns: int, state: dict) -> list[str]:
    """What code can check for sure."""
    out = []
    for i, l in enumerate(lines):
        if i and l["speaker"] == lines[i - 1]["speaker"]:
            out.append(f"line {i + 1}: the same host speaks twice in a row; alternate")
        if re.search(r"\bS\d+\b|https?://|www\.", l["text"]):
            out.append(f"line {i + 1}: a source id or web address is in the spoken text")
        if len(l["text"].split()) < 6:
            out.append(f"line {i + 1}: too short to be a turn")
    if len(lines) < max(2, turns // 2):
        out.append(f"only {len(lines)} lines; write {turns}")
    return out


def _clean_lines(out: dict, state: dict) -> list[dict]:
    lines = []
    for l in out.get("lines") or []:
        text = " ".join(re.sub(r"\[[^\]]*\]|\*+|#+", " ", str(l.get("text") or "")).split())
        if not text or l.get("speaker") not in ("A", "B"):
            continue
        words = text.split()
        while len(words) > MAX_LINE_WORDS:                    # split, never cut words off
            cut = MAX_LINE_WORDS
            for i in range(MAX_LINE_WORDS, MAX_LINE_WORDS // 2, -1):
                if words[i - 1].endswith((".", "?", "!")):
                    cut = i
                    break
            lines.append({"speaker": l["speaker"], "text": " ".join(words[:cut]),
                          "delivery": str(l.get("delivery") or "")[:80],
                          "cites": [c for c in l.get("cites") or [] if c in state["sources"]]})
            words = words[cut:]
        lines.append({"speaker": l["speaker"], "text": " ".join(words),
                      "delivery": str(l.get("delivery") or "")[:80],
                      "cites": [c for c in l.get("cites") or [] if c in state["sources"]]})
    return lines


# --------------------------------------------------------------------------- the editor feed

def _est(text: str) -> float:
    return len(text.split()) / WORDS_PER_S + 0.4


def plan_shots(lines: list[dict]) -> list[dict]:
    """Group a segment's lines into InfiniteTalk shots. A back-and-forth (lines of up to ~25 s,
    together up to ~50 s) is one `talk2` shot of both hosts -- each host's own track drives
    their own face, so it can hold A, B, A... (2026-11-27); a long turn is a `longtalk`
    close-up of the speaker."""
    from app.config import cfg as _cfg
    shots, i = [], 0
    while i < len(lines):
        group = [lines[i]]
        if _est(lines[i]["text"]) <= 25:
            j = i + 1
            while j < len(lines) and _est(lines[j]["text"]) <= 25 and \
                    sum(_est(x["text"]) for x in group) + _est(lines[j]["text"]) <= 50 and \
                    (_cfg.TALK_TOGETHER or len(group) < 2) and \
                    (len(group) > 1 or lines[j]["speaker"] != group[0]["speaker"]):
                group.append(lines[j])
                j += 1
        if len({x["speaker"] for x in group}) == 2:
            shots.append({"make": "talk2", "lines": group})
            i += len(group)
        else:
            shots.append({"make": "longtalk", "lines": [lines[i]]})
            i += 1
    return shots


def _story(config: dict, state: dict, title: str) -> str:
    a, b = config["hosts"]
    o = state["orchestrator"]
    out = [f"{title} — a two-host debate podcast (about {config['minutes']:g} minutes).",
           "Built by the podcast pipeline (web research, fact-checks, debate points, script): "
           "every line below is ALREADY under its shot in this production. The shots are "
           "InfiniteTalk talking shots (talk2 = both hosts in a 2S picture, longtalk = one host "
           "in close-up); keep them that way unless the person asks otherwise.",
           f"Topic: {config['topic']}",
           f"Hosts: {a['name']} ({a['lens']}) and {b['name']} ({b['lens']}).", "",
           "WHERE THEY COLLIDE (Tier 2):", o["summary"]]
    for p in o["points"]:
        out.append(f"- {p['title']}: {p['debateQuestion']}")
    out += ["", "SCRIPT:"]
    names = {"A": a["name"], "B": b["name"]}
    for seg in state["script"]:
        out.append(f"## {seg['title']}")
        for l in seg["lines"]:
            out.append(f"{names[l['speaker']]}: {l['text']}"
                       + (f" [{', '.join(l['cites'])}]" if l["cites"] else ""))
    out += ["", "FACT-CHECKS (after each researcher revised on its fact-checker's feedback):"]
    for side in ("a", "b"):
        for w in _final(state, side).get("withdrawn") or []:
            out.append(f"- [Withdrawn] {w['statement']} — {w.get('why') or w.get('status')}")
        for v in _final(state, side)["verdicts"]:
            ev = "; ".join(e["url"] for e in v["evidence"] if e.get("url"))
            out.append(f"- [{v['status']}] {v['statement']} — {v['note']}" + (f" ({ev})" if ev else ""))
    out += ["", "SOURCES (read from the web; quotes checked against the page):"]
    for s in state["sources"].values():
        out.append(f"{s['id']}: {s['title']} — {s['url']} — “{s['quote']}”"
                   + ("" if s["quote_ok"] else " (quote NOT found on the page)"))
    return "\n".join(out)


async def _build(pid: str, row, config: dict, state: dict) -> str:
    """The production: hosts (characters, looks drawn for the person to choose, voices
    designed from their description), the studio, a scene per segment, InfiniteTalk shots
    with their lines. Idempotent across restarts: the production id is saved first."""
    from app import agent, editor
    user = agent.load_user(row["user_id"])
    if user is None:
        raise RuntimeError("the account is gone")
    a, b = config["hosts"]
    title = config["title"] or state.get("title") or config["topic"][:80]
    project_id = row["project_id"]
    if project_id and db.one("SELECT 1 FROM shots WHERE project_id=?", (project_id,)):
        return project_id                                      # built before a restart
    if not project_id:
        project_id = editor.create(user, title, "", config["aspect"])
        _set(pid, project_id=project_id, title=title)
    project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    editor.set_story(project, title=title, story=_story(config, state, title))
    project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    refs = {}
    lib = {editor.name_key(x["name"]): x for x in editor.library(user)} if hasattr(editor, "library") else {}
    for key, h, other in (("A", a, b), ("B", b, a)):
        have = editor.ref_by_name(project_id, h["name"])
        src = lib.get(editor.name_key(h["name"]))
        if have:
            refs[key] = have["id"]
        elif src and src.get("kind") == "character":
            # One of the person's own characters: used as they are (look, views, voice).
            refs[key] = editor.import_ref(project, user, src["ref_id"])
            _log(pid, f"{h['name']}: your character from the library (look and voice as they are)")
        else:
            refs[key] = editor.add_ref(
                project, "character", h["name"],
                f"{h['look']}. A podcast host who argues from {h['lens']}; debates {other['name']}.",
                user=user, voice_design=h["voice"], approved=False)
    have = editor.ref_by_name(project_id, "The studio")
    src = lib.get(editor.name_key("The studio"))
    studio = have["id"] if have else (editor.import_ref(project, user, src["ref_id"])
                                      if src and src.get("kind") == "location" else
                                      editor.add_ref(project, "location", "The studio", STUDIO, user=user))
    everyone = [refs["A"], refs["B"], studio]
    names = {"A": a["name"], "B": b["name"]}
    looks = {"A": a["look"], "B": b["look"]}
    scenes = editor.scenes_of(project_id)
    for n, seg in enumerate(state["script"]):
        summary = (seg.get("point") or {}).get("debateQuestion") or \
            ("The hosts open the episode and frame the question." if seg["key"] == "opening"
             else "Final reflections and the sign-off.")
        if n == 0 and scenes:
            scene_id = scenes[0]["id"]
            editor.edit_scene(project, scene_id, seg["title"], summary, None, everyone)
        else:
            scene_id = editor.add_scene(project, seg["title"], summary, None, everyone)
        project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
        if n == 0:
            editor.add_shot(project, scene_id, "The studio",
                            f"Slow push-in on the podcast studio: {a['name']} on the left and "
                            f"{b['name']} on the right settle in at the table, adjust their "
                            "microphones and smile; warm light, a quiet moment before they begin.",
                            4, "WS", everyone, "animate", None, None, user)
            project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
        for k, shot in enumerate(plan_shots(seg["lines"])):
            l = shot["lines"]
            if shot["make"] == "talk2":
                gap = 0.25
                length = sum(_est(x["text"]) for x in l) + gap * (len(l) - 1)
                second = next(x["speaker"] for x in l if x["speaker"] != l[0]["speaker"])
                sid = editor.add_shot(
                    project, scene_id, f"{names[l[0]['speaker']]} and {names[second]}",
                    f"Two-shot at the podcast table: {a['name']} on the left and {b['name']} on "
                    "the right, facing each other mid-debate, studio microphones on boom arms, "
                    "seated and calm, hands resting, small nods, looking at each other; static camera.",
                    min(cfg.TALK_MAX_SECONDS, math.ceil(length + 1.2)), "2S",
                    everyone, "talk2", None, None, user)
                project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
                editor.edit_shot(project, sid, opts={
                    "layout": "left_right" if l[0]["speaker"] == "A" else "right_left"})
                at = 0.1
                for x in l:       # each line at its own moment; each host's lines are their track
                    editor.add_script(project, sid, x["text"], refs[x["speaker"]], x["delivery"], at)
                    at += _est(x["text"]) + gap
            else:
                who = l[0]["speaker"]
                sid = editor.add_shot(
                    project, scene_id, names[who],
                    f"Medium close-up of {names[who]} ({looks[who]}) speaking into a studio "
                    "microphone at the podcast table, expressive and engaged, natural hand "
                    "gestures, the studio softly blurred behind.",
                    min(cfg.TALK_MAX_SECONDS, math.ceil(_est(l[0]["text"]) + 1.2)), "MCU",
                    [refs[who], studio], "longtalk", None, None, user)
                project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
                editor.add_script(project, sid, l[0]["text"], refs[who], l[0]["delivery"], 0.1)
            project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    # The hosts' and the studio's pictures, for the person to choose (characters first).
    for rid in (refs["A"], refs["B"], studio):
        r = db.one("SELECT image_id FROM prod_refs WHERE id=?", (rid,))
        if r and not r["image_id"]:
            with suppress(Exception):
                await editor.make_ref_picture(project, user, rid)
    # Hosts whose looks are already chosen (the person's own characters): the shot pictures
    # start now; videos wait for the person to ask.
    if not editor.unchosen(project_id, [refs["A"], refs["B"]]):
        with suppress(Exception):
            editor.prepare_render(db.one("SELECT * FROM projects WHERE id=?", (project_id,)),
                                  user, None, "pictures")
            _log(pid, "The hosts' looks are already chosen: drawing the shot pictures now")
    return project_id


# --------------------------------------------------------------------------- the run

class _Turn:
    """The planner's GPU turn, taken only when a step needs the model and kept until the
    run stops at a gate or finishes."""

    def __init__(self, pid: str):
        self.pid, self.turn = pid, None

    async def get(self):
        from app import agent
        if self.turn is None:
            t = agent.GpuTurn(f"podcast:{self.pid}", say=lambda text: _set(self.pid, message=text))
            await t.acquire()
            self.turn = t
        return self.turn

    async def release(self):
        if self.turn is not None:
            t, self.turn = self.turn, None
            await t.release()


async def _yield(gpu: _Turn) -> None:
    """A chat with the assistant is waiting for the planner: let it go first (a run holds
    the planner for 10-25 minutes; 2026-11-26 a person's new chat sat behind one)."""
    from app import agent
    if gpu.turn is not None and agent.planner_wanted():
        await gpu.release()


async def _step(pid: str, state: dict, key: str, fn, needs_model: bool, gpu: _Turn):
    """Run one agent unless its output is already saved (resume after a restart)."""
    if key in state:
        return state[key]
    _check(pid)
    label = next((l for k, l, _ in STEPS if k == key), key)
    _log(pid, f"Started: {label}")
    turn = await gpu.get() if needs_model else None
    _set(pid, message=label + "…")
    out = await (fn(turn) if needs_model else fn())
    state[key] = out
    _save_state(pid, state)
    await _yield(gpu)
    prog = next((p for k, _, p in STEPS if k == key), None)
    if prog is not None:
        _set(pid, progress=prog)
    _log(pid, f"Completed: {label}")
    return out


async def _run(pid: str) -> None:
    gpu = _Turn(pid)
    try:
        while True:
            row = _row(pid)
            if row is None or row["status"] not in ACTIVE:
                return
            config, state = _config(row), _state(row)
            status = row["status"]
            if status in ("queued", "researching"):
                _set(pid, status="researching")
                await _step(pid, state, "show",
                            lambda t: _showrunner(pid, t, config, state), True, gpu)
                config = _config(_row(pid))            # the showrunner may have set the hosts
                q = await _step(pid, state, "queries",
                                lambda t: _queries(pid, t, config, state), True, gpu)
                if not config["title"] and q.get("title"):
                    state["title"] = q["title"]
                    _set(pid, title=q["title"])
                await _step(pid, state, "read", lambda: _read_web(pid, config, state), False, gpu)
                for side in ("a", "b"):
                    await _step(pid, state, f"{side}_collect",
                                lambda t, s=side: _collect(pid, t, config, state, s), True, gpu)
                    await _step(pid, state, f"{side}_synth",
                                lambda t, s=side: _synth(pid, t, config, state, s), True, gpu)
                    await _step(pid, state, f"{side}_check_pages",
                                lambda s=side: _check_pages(pid, config, state, s), False, gpu)
                    await _step(pid, state, f"{side}_fact",
                                lambda t, s=side: _fact(pid, t, config, state, s), True, gpu)
                    await _step(pid, state, f"{side}_revise",
                                lambda t, s=side: _revise(pid, t, config, state, s), True, gpu)
                    await _step(pid, state, f"{side}_final",
                                lambda t, s=side: _recheck(pid, t, config, state, s), True, gpu)
                    await _step(pid, state, f"{side}_persona",
                                lambda t, s=side: _persona(pid, t, config, state, s), True, gpu)
                if "research" in config["review"]:
                    await gpu.release()
                    _set(pid, status="review_research",
                         message="Review the research and both hosts' friction points, then continue")
                    _notify(pid, "is waiting for you to review the research")
                    return
                _set(pid, status="debating")
            elif status == "debating":
                await _step(pid, state, "orchestrator",
                            lambda t: _orchestrate(pid, t, config, state), True, gpu)
                if "points" in config["review"] and not state.get("points_reviewed"):
                    await gpu.release()
                    _set(pid, status="review_points",
                         message="Review or edit the debate points, then continue")
                    _notify(pid, "is waiting for you to review the debate points")
                    return
                _set(pid, status="writing")
            elif status == "writing":
                if "script" not in state:
                    segs = _segments(config, state["orchestrator"]["points"])
                    done = state.setdefault("script_parts", [])
                    for n, seg in enumerate(segs):
                        if n < len(done):
                            continue
                        _check(pid)
                        turn = await gpu.get()
                        _set(pid, message=f"Tier 3 · Writing “{seg['title']}” ({n + 1}/{len(segs)})…")
                        prev = [l for d in done for l in d["lines"]]
                        lines = await _write_segment(pid, turn, config, state, seg, prev)
                        done.append({"key": seg["key"], "title": seg["title"],
                                     "point": seg.get("point"), "lines": lines})
                        _save_state(pid, state)
                        _set(pid, progress=75 + int(17 * (n + 1) / len(segs)))
                        _log(pid, f"Script: “{seg['title']}” — {len(lines)} lines")
                        await _yield(gpu)
                    state["script"] = done
                    state.pop("script_parts", None)
                    _save_state(pid, state)
                    words = sum(len(l["text"].split()) for s in done for l in s["lines"])
                    _log(pid, f"Completed: Tier 3 script — {sum(len(s['lines']) for s in done)} "
                              f"lines, {words} words (~{words / WORDS_PER_MIN:.1f} min)")
                await gpu.release()
                _set(pid, status="building")
            elif status == "building":
                await gpu.release()
                _set(pid, message="Building the production in the editor…")
                project_id = await _build(pid, row, config, state)
                state["built"] = project_id
                _save_state(pid, state)
                _set(pid, status="done", progress=100, project_id=project_id,
                     message="Ready in the editor: choose the hosts' looks, then draw and render")
                _log(pid, "Completed: the production is in the editor — hosts, studio, "
                          f"{len(state['script'])} scenes, InfiniteTalk shots with their lines")
                _notify(pid, "is ready in the editor")
                return
    except (Cancelled, asyncio.CancelledError):
        _log(pid, "Stopped; the GPU is free")
    except Exception as exc:                                   # noqa: BLE001
        log.exception("podcast %s failed", pid)
        row = _row(pid)
        _set(pid, status="failed", error=str(exc)[:500],
             message=f"Stopped: {str(exc)[:200]}",
             config=json.dumps({**_config(row), "failed_at": row["status"]}) if row else "{}")
        _log(pid, f"Failed: {exc}")
        _notify(pid, f"stopped: {str(exc)[:200]}")
    finally:
        await gpu.release()


def _notify(pid: str, what: str) -> None:
    """Tell the assistant conversation that asked for this podcast."""
    row = _row(pid)
    if row is None or not row["thread_id"]:
        return
    with suppress(Exception):
        from app import agent
        agent._queue_turn(row["thread_id"], f"review: the podcast “{row['title'] or row['topic'][:60]}” "
                                            f"({pid}) {what} -- tell the person")


_wake = asyncio.Event()


def kick() -> None:
    _wake.set()


async def worker() -> None:
    """One podcast at a time (one planner model, one GPU). Runs in progress when the
    studio restarted carry on from their last saved step."""
    while True:
        try:
            row = db.one("SELECT id FROM podcasts WHERE status IN (%s) ORDER BY created_at LIMIT 1"
                         % ",".join("?" * len(ACTIVE)), ACTIVE)
            if row is None:
                _wake.clear()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(_wake.wait(), timeout=5)
                continue
            task = asyncio.create_task(_run(row["id"]))
            _current.update(id=row["id"], task=task)
            try:
                await task
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise                      # the worker itself is stopping
                log.info("podcast %s stopped", row["id"])
            finally:
                _current.clear()
        except asyncio.CancelledError:
            raise
        except Exception:                                      # noqa: BLE001
            log.exception("podcast worker survived an error")
            await asyncio.sleep(5)


# --------------------------------------------------------------------------- actions

def proceed(user: auth.User, podcast_id: str, points: list | None = None) -> dict:
    """Continue from a review gate; at the points gate the person's edited points win."""
    row = owned(user, podcast_id)
    state = _state(row)
    if row["status"] == "review_research":
        _set(podcast_id, status="debating", message="continuing…")
    elif row["status"] == "review_points":
        if isinstance(points, list):
            clean = [{k: " ".join(str(p.get(k) or "").split())[:700]
                      for k in ("title", "philosophicalVulnerability", "aPosition", "bPosition",
                                "factualGround", "debateQuestion")}
                     for p in points if isinstance(p, dict) and str(p.get("title") or "").strip()]
            if not clean:
                raise HTTPException(400, "keep at least one debate point")
            state["orchestrator"]["points"] = clean[:6]
            _log(podcast_id, f"Debate points edited by the person ({len(clean)})")
        state["points_reviewed"] = True
        _save_state(podcast_id, state)
        _set(podcast_id, status="writing", message="continuing…")
    else:
        raise HTTPException(409, "this podcast is not waiting for a review")
    kick()
    return view(_row(podcast_id))


_current: dict = {}          # the run in progress: {"id", "task"}


def _stop_now(podcast_id: str, why: str) -> None:
    """Cancelled: mark it, and stop the run at once (mid model call too), freeing the GPU."""
    _set(podcast_id, status="cancelled", message="Cancelled")
    _log(podcast_id, why)
    if _current.get("id") == podcast_id and _current.get("task") is not None:
        _current["task"].cancel()


def cancel(user: auth.User, podcast_id: str) -> dict:
    row = owned(user, podcast_id)
    if row["status"] in FINAL:
        raise HTTPException(409, f"this podcast is already {row['status']}")
    _stop_now(podcast_id, "Cancelled by the person")
    return view(_row(podcast_id))


def cancel_for_thread(thread_id: str) -> int:
    """The assistant conversation that started these was stopped or deleted."""
    rows = db.query("SELECT id FROM podcasts WHERE thread_id=? AND status IN (%s)"
                    % ",".join("?" * (len(ACTIVE) + len(GATES))), (thread_id, *ACTIVE, *GATES))
    for r in rows:
        _stop_now(r["id"], "Cancelled: the conversation that asked for it was stopped")
    return len(rows)


def retry(user: auth.User, podcast_id: str) -> dict:
    """Start a failed or cancelled run again from where it stopped (saved steps are kept)."""
    row = owned(user, podcast_id)
    if row["status"] not in ("failed", "cancelled"):
        raise HTTPException(409, "only a stopped podcast can be retried")
    config = _config(row)
    back = config.pop("failed_at", None)
    state = _state(row)
    if back not in ACTIVE:
        back = ("building" if "script" in state else "writing" if "orchestrator" in state
                else "debating" if "b_persona" in state else "researching")
    db.run("UPDATE podcasts SET status=?, error=NULL, config=?, updated_at=? WHERE id=?",
           (back, json.dumps(config), db.now(), podcast_id))
    _log(podcast_id, f"Retrying from: {back}")
    kick()
    return view(_row(podcast_id))


def delete(user: auth.User, podcast_id: str) -> None:
    row = owned(user, podcast_id)
    if row["status"] in ACTIVE:
        raise HTTPException(409, "cancel it first")
    db.run("DELETE FROM podcasts WHERE id=?", (podcast_id,))


def purge_older_than(cutoff: float) -> None:
    db.run("DELETE FROM podcasts WHERE updated_at < ? AND status IN ('done','failed','cancelled')",
           (cutoff,))


def view(row, full: bool = True) -> dict:
    config, state = _config(row), _state(row)
    a, b = config.get("hosts") or DEFAULT_HOSTS
    out = {"podcast_id": row["id"], "title": row["title"] or state.get("title") or "",
           "topic": row["topic"], "status": row["status"], "message": row["message"],
           "progress": row["progress"], "hosts": [a, b], "minutes": config.get("minutes"),
           "aspect": config.get("aspect"), "review": config.get("review") or [],
           "created_at": row["created_at"], "updated_at": row["updated_at"],
           **service.retention(row["updated_at"])}
    if row["error"]:
        out["error"] = row["error"]
    if row["project_id"]:
        out["project_id"] = row["project_id"]
        out["editor_path"] = f"/edit/{row['project_id']}"
    if not full:
        return out
    out["steps"] = [{"key": k, "label": l, "done": k in state} for k, l, _ in STEPS]
    if "queries" in state:
        out["queries"] = state["queries"]
    out["sources"] = list((state.get("sources") or {}).values())
    for side in ("a", "b"):
        if f"{side}_synth" in state:
            out[f"{side}_synthesis"] = state[f"{side}_synth"]
        if f"{side}_fact" in state:
            out[f"{side}_factcheck"] = _final(state, side)
            out[f"{side}_factcheck"]["revised"] = f"{side}_final" in state
        if f"{side}_persona" in state:
            out[f"{side}_persona"] = state[f"{side}_persona"]
    if "orchestrator" in state:
        out["orchestrator"] = state["orchestrator"]
    script = state.get("script") or state.get("script_parts")
    if script:
        out["script"] = [{"title": s["title"], "lines": s["lines"]} for s in script]
    try:
        out["log"] = json.loads(row["log"] or "[]")[-80:]
    except ValueError:
        out["log"] = []
    out["pages_read"] = len(state.get("pages") or {})
    return out


# --------------------------------------------------------------------------- routes

def _user(request: Request) -> auth.User:
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(401, "sign in first")
    return user


@router.get("/podcast", response_class=HTMLResponse)
async def page(request: Request):
    from app.main import templates
    user = auth.current_user(request)
    if user is None:
        return RedirectResponse("/login?next=/podcast", status_code=303)
    rows = db.query("SELECT * FROM podcasts WHERE user_id=? ORDER BY created_at DESC LIMIT 40",
                    (user.id,))
    return templates.TemplateResponse("podcast.html", {
        "request": request, "user": user, "runs": [view(r, full=False) for r in rows],
        "lenses": LENSES, "hosts": DEFAULT_HOSTS, "per_day": cfg.USER_PODCASTS_PER_DAY,
        "unlimited": auth.is_unlimited(user.id)})


@router.get("/podcast/{podcast_id}", response_class=HTMLResponse)
async def run_page(podcast_id: str, request: Request):
    from app.main import templates
    user = auth.current_user(request)
    if user is None:
        return RedirectResponse(f"/login?next=/podcast/{podcast_id}", status_code=303)
    row = owned(user, podcast_id)
    return templates.TemplateResponse("podcast_run.html", {
        "request": request, "user": user, "run": view(row)})


@router.post("/api/podcasts")
async def api_create(request: Request):
    user = _user(request)
    body = await request.json()
    return view(_row(create(user, body if isinstance(body, dict) else {})))


@router.get("/api/podcasts/{podcast_id}")
async def api_get(podcast_id: str, request: Request):
    return view(owned(_user(request), podcast_id))


@router.post("/api/podcasts/{podcast_id}/continue")
async def api_continue(podcast_id: str, request: Request):
    user = _user(request)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    return proceed(user, podcast_id, (body or {}).get("points"))


@router.post("/api/podcasts/{podcast_id}/cancel")
async def api_cancel(podcast_id: str, request: Request):
    return cancel(_user(request), podcast_id)


@router.post("/api/podcasts/{podcast_id}/retry")
async def api_retry(podcast_id: str, request: Request):
    return retry(_user(request), podcast_id)


@router.post("/api/podcasts/{podcast_id}/delete")
async def api_delete(podcast_id: str, request: Request):
    delete(_user(request), podcast_id)
    return {"ok": True}
