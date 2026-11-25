"""The studio assistant: a planner, small executors, and a watcher.

The person talks to it in plain language on /assistant. Three roles, on purpose:

- PLANNER -- the strongest local model (cfg.PLANNER_MODEL, qwen3.8:27b). It reads
  the conversation, the person's standing lessons, and what the studio is doing,
  and answers with a reply plus a list of tool steps. It needs ~20 GB of the one
  24 GB card, so it runs ONLY inside a GPU turn: the studio's own speech/music
  queue is paused, a WIS GPU lease is taken (WIS finishes the job in hand, then
  unloads its models and holds everything else back), the planner runs, and is
  unloaded before the lease goes back. GPU work never runs while it thinks.

- EXECUTORS -- a small model (cfg.EXECUTOR_MODEL) on the CPU (num_gpu 0), so it
  never competes with renders. The planner may leave arguments for it to fill
  ("speak the narration from step 4, calm"); the executor turns that into an
  exact call against the one tool's schema, and when a call is refused it reads
  the error and tries a corrected call. Steps whose arguments the planner gave
  completely run directly -- re-typing a correct call adds nothing but risk.

- WATCHER -- plain code, every cfg.AGENT_POLL_S: checks delegated work (renders,
  queued pictures, speech, songs, voice jobs). The planner is called back to
  review when something finishes or fails, and at least every cfg.AGENT_REVIEW_S
  while work runs. On review it SEES finished pictures and a frame of finished
  videos, and either reports to the person or corrects (a bounded number of
  rounds, cfg.AGENT_MAX_CORRECTIONS, before it must ask).

The tools are the MCP tools (app/mcp.py), called in-process as the signed-in
person, so every rule in app/service.py applies exactly as it does for the pages
and for outside assistants. Two extra tools exist only here: studio_status and
retry_job, for diagnosing and repairing the person's own failed work.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import datetime as _dt
import inspect
import json
import logging
import re
import subprocess
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request

from app import agentfiles, auth, db, editor, gpuqueue, mcp, render, service, wis
from app.config import cfg

log = logging.getLogger("studio.agent")
router = APIRouter()

MAX_ROUNDS = 6                 # planner calls per turn (continue / self-fix rounds)
EXECUTOR_TRIES = 3             # calls per step, including executor repairs
# Renewed before every planner call, so this only bounds how long a lease outlives a
# crashed or restarted process. It was 900 s: a deploy mid-turn left the card held,
# idle, for up to 15 minutes (2026-11-24). The lease id is also kept on disk and
# returned at startup (see _remember_lease / release_leftover_lease).
LEASE_HOLD_S = 240
TERMINAL = {"succeeded", "failed", "cancelled", "canceled", "error", "done"}
# Tools that put work on the GPU (or wait on WIS, which queues behind the
# planner's lease): the planner's GPU turn must end before they run, or they
# would wait for a lease that waits for them.
HEAVY = {"generate_image", "generate_speech", "generate_music", "render_video", "plan_scenes",
         "resing_song", "sing_song", "speak_as_voice", "train_voice", "retry_job",
         *editor.EDITOR_HEAVY}
# Raw-data arguments only a chat client with the file in hand can send.
HIDDEN_ARGS = {"file", "source_file"}   # chat-client file handles; "@F<n>" replaces them here
# Where each kind of delegated work is checked, and which id it is checked by.
WATCH = {"project": ("get_status", "project_id"), "image": ("get_image", "image_id"),
         "speech": ("get_speech", "speech_id"), "music": ("get_music", "music_id"),
         "voice_job": ("get_voice_job", "job_id")}

CREATES = {"generate_image": "image", "generate_speech": "speech", "generate_music": "music",
           "resing_song": "voice_job", "sing_song": "voice_job", "speak_as_voice": "voice_job",
           "train_voice": "voice_job"}

_wake = asyncio.Event()
_active: dict[str, asyncio.Task] = {}
_views: dict[str, list] = collections.defaultdict(list)   # pictures for the next review
_turns: dict[str, collections.deque] = collections.defaultdict(collections.deque)


# =========================================================================== storage

def _now() -> float:
    return db.now()


def _thread(thread_id: str):
    return db.one("SELECT * FROM agent_threads WHERE id=?", (thread_id,))


def owned_thread(user: auth.User, thread_id: str):
    row = _thread(thread_id)
    if row is None or row["user_id"] != user.id:
        raise HTTPException(status_code=404, detail="no such conversation")
    return row


def _set(thread_id: str, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    db.run(f"UPDATE agent_threads SET {sets}, updated_at=? WHERE id=?",
           (*fields.values(), _now(), thread_id))


def _post(thread_id: str, role: str, content: str = "", **meta) -> int:
    now = _now()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO agent_messages (thread_id, role, content, meta, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)", (thread_id, role, content, json.dumps(meta), now, now))
        conn.execute("UPDATE agent_threads SET updated_at=? WHERE id=?", (now, thread_id))
        return cur.lastrowid


def _patch(seq: int, content: str | None = None, **meta) -> None:
    row = db.one("SELECT content, meta FROM agent_messages WHERE seq=?", (seq,))
    if row is None:
        return
    merged = {**json.loads(row["meta"] or "{}"), **meta}
    db.run("UPDATE agent_messages SET content=?, meta=?, updated_at=? WHERE seq=?",
           (row["content"] if content is None else content, json.dumps(merged), _now(), seq))


def _activity(thread_id: str, text: str, status: str | None = None) -> None:
    extra = {}
    if status:
        row = _thread(thread_id)
        extra["status"] = status
        if row is None or row["status"] != status:
            extra["stage_at"] = _now()
    _set(thread_id, activity=text, thought="", **extra)


def _tracked(row) -> list[dict]:
    try:
        return json.loads(row["tracked"] or "[]")
    except ValueError:
        return []


def load_user(user_id: str) -> auth.User | None:
    row = db.one("SELECT id, username, role, expires_at, disabled FROM users WHERE id=?",
                 (user_id,))
    if row is None or row["disabled"] or (row["expires_at"] and row["expires_at"] < _now()):
        return None
    return auth.User(row["id"], row["username"], row["role"], row["expires_at"])


# A lesson belongs to the person (project_id NULL: every story) or to one story.
# On 2026-11-24 a Rāmāyaṇa series' rules ("2D hand-drawn animation", unstitched
# costumes) were all saved for every story, and the planner drew a modern
# romance as a cartoon with its heroine in a wrapped cloth and uttarīya.
def lessons(user_id: str, project_id: str | None = None, everything: bool = True) -> list:
    """The person's lessons. everything=False: only the ones that apply in project_id
    (the person's general ones plus that story's own)."""
    rows = db.query("SELECT l.*, p.title AS project_title FROM agent_lessons l "
                    "LEFT JOIN projects p ON p.id = l.project_id "
                    "WHERE l.user_id=? ORDER BY l.created_at", (user_id,))
    if everything:
        return rows
    return [r for r in rows if not r["project_id"] or r["project_id"] == project_id]


def _lesson_text(r) -> str:
    if r["project_id"]:
        return f"[only for “{r['project_title'] or 'a deleted story'}”] {r['text']}"
    return r["text"]


def _add_lesson(user_id: str, text: str, project_id: str | None = None) -> None:
    text = " ".join(str(text).split())[:300]
    if not text:
        return
    have = {r["text"].lower() for r in lessons(user_id)}
    if text.lower() in have or len(have) >= 40:
        return
    db.run("INSERT INTO agent_lessons (id, user_id, text, created_at, project_id) "
           "VALUES (?,?,?,?,?)", (db.new_id("les"), user_id, text, _now(), project_id))


def kick() -> None:
    _wake.set()


def _queue_turn(thread_id: str, reason: str) -> None:
    _set(thread_id, status="queued", reason=reason, activity="waiting for the planner",
         stage_at=_now(), thought="")
    kick()


def purge_older_than(cutoff: float) -> None:
    """Retention: conversations go 90 days after they last changed. Lessons stay
    until the person deletes them (they are preferences, not content)."""
    for row in db.query("SELECT id FROM agent_threads WHERE updated_at < ? AND status IN "
                        "('idle','stopped')", (cutoff,)):
        agentfiles.delete_for_thread(row["id"])
        db.run("DELETE FROM agent_threads WHERE id=?", (row["id"],))


# =========================================================================== tools

def _studio_status(user, args, base):
    day = _now() - 86400
    failed = []
    for table, key, label in (("projects", "project_id", "video"), ("images", "image_id", "picture"),
                              ("speech", "speech_id", "speech"), ("music", "music_id", "song")):
        for r in db.query(f"SELECT id, error, updated_at FROM {table} WHERE user_id=? AND "
                          "status='failed' AND updated_at > ? ORDER BY updated_at DESC LIMIT 5",
                          (user.id, day)):
            failed.append({"kind": label, key: r["id"], "error": (r["error"] or "")[:300]})
    queued = {t: db.one(f"SELECT COUNT(*) AS n FROM {t} WHERE status IN ('queued','generating')")
              ["n"] for t in gpuqueue.TABLES}
    drawing = {}
    for p in db.query("SELECT id, title FROM projects WHERE user_id=? AND params_json LIKE "
                      "'%_auto_shots%'", (user.id,)):
        prog = editor.drawing_progress(p["id"])
        if prog:
            drawing[p["title"]] = prog
    return {"video_renders_waiting": render.queue_depth(), "speech_music_voice_queue": queued,
            # pictures being drawn per production (the GPU works through them one by one; do
            # not ask again -- 2026-11-25 the planner re-triggered twice thinking nothing ran)
            "pictures_being_drawn": drawing,
            "your_failures_last_24h": failed,
            "advice": "Read each error. Fix the cause (shorter scene, other model, smaller size, "
                      "missing picture) and re-run with the corrected call; retry_job re-queues a "
                      "failed speech, song or voice job unchanged (for a GPU-full failure)."}


def _retry_job(user, args, base):
    kind, job_id = str(args.get("kind") or ""), str(args.get("id") or "")
    table = {"speech": "speech", "music": "music", "voice_job": "voice_jobs"}.get(kind)
    if table is None:
        raise HTTPException(400, "kind must be speech, music or voice_job; to redo a video call "
                                 "render_video, to redo a picture call generate_image again")
    row = db.one(f"SELECT id, user_id, status FROM {table} WHERE id=?", (job_id,))
    if row is None or row["user_id"] != user.id:
        raise HTTPException(404, f"no such {kind}")
    if row["status"] != "failed":
        raise HTTPException(409, f"that {kind} is {row['status']}, not failed")
    db.run(f"UPDATE {table} SET status='queued', error=NULL, attempts=0, created_at=?, "
           "updated_at=? WHERE id=?", (_now(), _now(), job_id))
    gpuqueue.kick()
    return {"id": job_id, "kind": kind, "status": "queued"}


def _strip(schema: dict) -> dict:
    props = {}
    for k, v in (schema.get("properties") or {}).items():
        if k in HIDDEN_ARGS:
            continue
        if k.endswith("base64"):
            v = {"type": "string", "description": "an attached file: pass \"@F<n>\" (its bytes "
                                                  "are sent for you)"}
        props[k] = v
    return {**schema, "properties": props,
            "required": [r for r in schema.get("required", []) if r in props]}


def _build_tools() -> dict:
    out = {}
    for t in mcp.TOOLS:
        spec = t["spec"]
        if spec["name"] in ("get_upload_link", "get_voice_upload_link"):
            continue    # the person uploads on the project page; there is no shell here
        out[spec["name"]] = {"handler": t["handler"], "description": spec["description"],
                             "schema": _strip(spec["inputSchema"])}
    out["studio_status"] = {
        "handler": _studio_status, "schema": {"type": "object", "properties": {}, "required": []},
        "description": "What the studio is doing now (queues) and the person's own failures in "
                       "the last 24 hours with their error messages. Use it to diagnose."}
    out["read_file"] = {
        "handler": None, "light": True,
        "schema": {"type": "object", "required": ["file"], "properties": {
            "file": {"type": "string", "description": "F1, F2 ..."},
            "start": {"type": "integer", "description": "character offset"},
            "length": {"type": "integer", "description": "characters, up to 20000"}}},
        "description": "Read more of an attached document's text or a recording's transcript "
                       "than the excerpt you were shown."}
    out["retry_job"] = {
        "handler": _retry_job,
        "schema": {"type": "object", "required": ["kind", "id"], "properties": {
            "kind": {"type": "string", "enum": ["speech", "music", "voice_job"]},
            "id": {"type": "string"}}},
        "description": "Re-queue a FAILED speech clip, song or voice job unchanged. Only for "
                       "failures that were not caused by the request itself (e.g. GPU full)."}
    for name, t in editor.AGENT_TOOLS.items():
        # research tools serve the general assistant too; the rest need a production
        out[name] = {**t, "editor": name not in ("web_search", "read_web_page")}
    return out


_TOOLS: dict | None = None


def tools() -> dict:
    global _TOOLS
    if _TOOLS is None:
        _TOOLS = _build_tools()
    return _TOOLS


def _arg_line(name: str, prop: dict, required: bool) -> str:
    kind = prop.get("type", "any")
    if kind == "array":
        kind = f"array of {(prop.get('items') or {}).get('type', 'any')}"
    if prop.get("enum"):
        kind += " one of " + "|".join(map(str, prop["enum"]))
    desc = " ".join(str(prop.get("description", "")).split())[:160]
    return f"    {name}{'*' if required else ''}: {kind}" + (f" -- {desc}" if desc else "")


# Tools of the older project (MCP) workflow that a production never uses; leaving them out of
# a production's catalogue saves ~4,500 tokens per planner call (2026-11-25: a production's
# prompt had grown to 30,056 tokens in a 32,768 window, leaving the planner no room to think).
NOT_IN_PRODUCTION = {
    "create_project", "update_project", "get_project", "list_projects", "render_video",
    "cancel_render", "get_status", "plan_scenes", "set_scenes", "set_scene_video",
    "set_scene_voice", "add_file", "add_media", "generate_music", "get_music",
    "generate_speech", "get_speech", "list_speech", "sing_song", "resing_song",
    "speak_as_voice", "train_voice", "get_voice_job",
}


def catalogue(production: bool = False) -> str:
    lines = []
    for name, t in tools().items():
        if t.get("editor") and not production:
            continue
        if production and name in NOT_IN_PRODUCTION:
            continue
        desc = " ".join(t["description"].split())[:420]
        lines.append(f"- {name}: {desc}")
        req = set(t["schema"].get("required", []))
        for arg, prop in (t["schema"].get("properties") or {}).items():
            lines.append(_arg_line(arg, prop, arg in req))
    return "\n".join(lines)


def _validate(args: dict, schema: dict) -> tuple[dict, list[str]]:
    """Drop unknown keys; list what is missing or wrong (the executor fixes those)."""
    props = schema.get("properties") or {}
    clean = {k: v for k, v in (args or {}).items() if k in props and v is not None}
    problems = []
    for r in schema.get("required", []):
        if r not in clean or clean[r] in ("", [], {}):
            problems.append(f"{r} is required")
    types = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
             "array": list, "object": dict}
    for k, v in clean.items():
        p = props[k]
        want = types.get(p.get("type"))
        if isinstance(v, str) and _REF.search(v):
            problems.append(f"{k} still refers to an earlier step ({v})")
        elif want and not isinstance(v, want) or (p.get("type") == "integer" and isinstance(v, bool)):
            problems.append(f"{k} must be {p.get('type')}")
        elif p.get("enum") and v not in p["enum"]:
            problems.append(f"{k} must be one of {p['enum']}")
    return clean, problems


_REF = re.compile(r"\$(\d+)\.([A-Za-z_][\w.]*)")


def _lookup(result, path: str):
    cur = result
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _resolve(value, results: dict):
    """Fill `$7.image_id` references from earlier step results (plain code)."""
    if isinstance(value, dict):
        return {k: _resolve(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, results) for v in value]
    if isinstance(value, str):
        whole = _REF.fullmatch(value.strip())
        if whole:
            got = _lookup(results.get(int(whole.group(1))), whole.group(2))
            return value if got is None else got

        def sub(m):
            got = _lookup(results.get(int(m.group(1))), m.group(2))
            return m.group(0) if got is None or isinstance(got, (dict, list)) else str(got)
        return _REF.sub(sub, value)
    return value


async def _run_tool(user, name: str, args: dict, base: str,
                    thread_id: str = "") -> tuple[bool, dict, bytes | None]:
    """(ok, result, preview_jpeg). A refusal is a result, not an exception."""
    tool = tools()[name]
    try:
        if name == "read_file":
            return True, agentfiles.read_text(thread_id, str(args.get("file", "")),
                                              args.get("start", 0), args.get("length", 12000)), None
        if thread_id:
            args = agentfiles.materialize(thread_id, agentfiles.filename_for(thread_id, args))
        out = tool["handler"](user, args, base)
        if inspect.isawaitable(out):
            out = await out
        preview = out.pop("_preview", None) if isinstance(out, dict) else None
        return True, out if isinstance(out, dict) else {"result": out}, preview
    except HTTPException as exc:
        return False, {"error": str(exc.detail)}, None
    except Exception as exc:                                   # noqa: BLE001
        log.exception("agent tool %s failed", name)
        return False, {"error": f"internal error: {type(exc).__name__}"}, None


# =========================================================================== models

async def _ollama(path: str, payload: dict, timeout: float = 900.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{cfg.OLLAMA_URL}{path}", json=payload)
    if resp.status_code >= 400:
        raise RuntimeError(f"ollama {path}: {resp.status_code} {resp.text[:300]}")
    return resp.json()


async def _unload(model: str) -> None:
    try:
        await _ollama("/api/generate", {"model": model, "keep_alive": 0}, timeout=60)
        for _ in range(30):
            async with httpx.AsyncClient(timeout=10) as client:
                ps = (await client.get(f"{cfg.OLLAMA_URL}/api/ps")).json()
            if not any(m.get("name") == model or m.get("model") == model
                       for m in ps.get("models") or []):
                return
            await asyncio.sleep(1)
        log.warning("%s still loaded 30 s after unload", model)
    except Exception as exc:                                   # noqa: BLE001
        log.warning("could not unload %s: %s", model, exc)


# qwen3.8:27b Q4 at a 32k context: measured ~20 GB on the card.
PLANNER_VRAM_MB = 20500

PRODUCTION_ABOUT = ("Story Studio makes films on one local GPU: pictures (FLUX.2 klein, Qwen-Image), "
                    "video clips per shot (the model of each shot's use case), voices (Qwen3-TTS; "
                    "VoxCPM2 for Burmese and Hindi voices), music and ambience (ACE-Step). Work "
                    "only through the production tools below; files are kept 90 days.")

# The planner's context window. Measured 2026-11-25 on the RTX 4090 with qwen3.8:27b
# (hybrid attention: ~1.1 GB per extra 32k tokens, but Ollama's placement reserve grows too):
#   40960 -> fully on the GPU, 116 tok/s      49152 -> 15.8/17.0 GB on GPU, 94 tok/s
#   57344+ -> part on the CPU, ~35 tok/s      forcing num_gpu at 65536 ran out of memory.
# So each call takes the SMALLEST window that holds its prompt plus the thinking budget.
PLANNER_WINDOWS = (40960, 49152, 65536)
PLANNER_THINK_TOKENS = 20000          # num_predict for a breakdown: a whole episode used 18.5k
                                      # and 26.5k (12k cut the first try off)
PLANNER_THINK_LATER = 12000           # once the production has shots: later turns used 1.3k-6.1k
PLANNER_THINK_MAX = 32000             # a turn cut off for length retries with double, up to this


def think_budget(project_id: str | None) -> int:
    """Room to think: big while the story is still being broken down, smaller afterwards
    (so later prompts fit the fast window). A turn cut off for length is retried bigger."""
    if project_id and db.one("SELECT 1 FROM shots WHERE project_id=? LIMIT 1", (project_id,)):
        return PLANNER_THINK_LATER
    return PLANNER_THINK_TOKENS
CHARS_PER_TOKEN = 3.0                 # measured 3.29 on a Sanskrit-heavy production; err high


def _est_tokens(messages: list[dict]) -> int:
    n = sum(len(m.get("content") or "") for m in messages) / CHARS_PER_TOKEN
    return int(n + 800 * sum(len(m.get("images") or []) for m in messages) + 50 * len(messages))


COMPACT_LEVELS = 4
_STEP = re.compile(r"^#(\d+) (\S+) \[(\w+)\]")


def fold_log(content: str, keep_last: int = 2) -> str:
    """A studio-log message with runs of the same tool folded into one line:
    "#12-#35 make_ref_picture x24 [ok 22, running 2]". Failed steps and the last
    `keep_last` lines stay in full (2026-11-25: 25 make_ref_picture lines were 6.6k tokens)."""
    head, _, body = content.partition("\n")
    lines = body.split("\n")
    out, run = [], []

    def flush():
        if len(run) >= 3:
            counts: dict[str, int] = {}
            for _, st, _ln in run:
                counts[st] = counts.get(st, 0) + 1
            n0, n1 = run[0][0], run[-1][0]
            tool = _STEP.match(run[0][2]).group(2)
            out.append(f"#{n0}-#{n1} {tool} x{len(run)} [" +
                       ", ".join(f"{k} {v}" for k, v in counts.items()) + "]")
            out.extend(ln for _, st, ln in run if st not in ("ok", "done", "running", "queued"))
        else:
            out.extend(ln for _, _, ln in run)
        run.clear()

    tail = lines[-keep_last:] if keep_last else []
    for ln in lines[:len(lines) - len(tail)]:
        m = _STEP.match(ln)
        if m and run and _STEP.match(run[-1][2]).group(2) == m.group(2):
            run.append((m.group(1), m.group(3), ln))
            continue
        flush()
        if m:
            run.append((m.group(1), m.group(3), ln))
        else:
            out.append(ln[:240])
    flush()
    return head + "\n" + "\n".join(out + tail)


def compact_history(history: list[dict], level: int) -> list[dict]:
    """level >=1: studio logs folded; >=4: the assistant's older replies shortened. The
    person's own messages are never changed: they are the instructions."""
    if level <= 0:
        return history
    out = []
    replies = [i for i, m in enumerate(history) if m["role"] == "assistant"]
    for i, m in enumerate(history):
        m = dict(m)
        if m["role"] == "user" and (m.get("content") or "").startswith("[studio log]"):
            m["content"] = fold_log(m["content"])
        elif level >= 4 and m["role"] == "assistant" and i not in replies[-2:] \
                and len(m.get("content") or "") > 400:
            m["content"] = m["content"][:400] + " ..."
        out.append(m)
    return out


def _fit_window(messages: list[dict], think: int = PLANNER_THINK_TOKENS
                ) -> tuple[list[dict], int, int]:
    """(messages, num_ctx, estimated prompt tokens): the smallest measured window that holds
    the prompt + `think`; if none does, the oldest history (never the system
    prompt, the latest message or the person's last three messages) is dropped until it fits.
    Ollama would otherwise cut the START of the prompt silently -- the rules."""
    msgs = list(messages)
    margin = 1024
    while True:
        est = _est_tokens(msgs)
        for ctx in PLANNER_WINDOWS:
            if est + think + margin <= ctx:
                return msgs, ctx, est
        last = len(msgs) - 1
        keep = {0, last} | set([i for i, m in enumerate(msgs[:last]) if m["role"] == "user"][-3:])
        drop = next((i for i in range(1, last) if i not in keep), None)
        if drop is None:
            return msgs, PLANNER_WINDOWS[-1], est
        msgs.pop(drop)


class GpuTurn:
    """Hold the card for the planner: pause the studio queue, take a WIS lease."""

    def __init__(self, thread_id: str):
        self.thread_id, self.lease, self.held = thread_id, None, False

    async def acquire(self) -> None:
        gpuqueue.pause()
        self.held = True
        try:
            waited = 0
            while not gpuqueue.idle():
                _activity(self.thread_id, "waiting for a speech/music job on the GPU to finish, "
                                          "then the planner takes its turn")
                await asyncio.sleep(2)
                waited += 2
            _activity(self.thread_id, "waiting for a turn on the GPU (the picture or video "
                                      "shot in progress finishes first; new GPU work is held)")
            while True:
                try:
                    self.lease = await wis.lease_gpu("studio-planner", LEASE_HOLD_S)
                    if self.lease:
                        _remember_lease(self.lease)
                except Exception as exc:                       # noqa: BLE001
                    log.warning("no WIS lease for the planner (%s); running without", exc)
                    self.lease = ""
                if self.lease is not None:
                    break
                await asyncio.sleep(3)
            await self._wait_for_room()
        except BaseException:
            await self.release()
            raise

    async def _wait_for_room(self) -> None:
        """The lease means WIS has stopped, not that the card is already empty.

        On 2026-11-24 the planner was handed the card while a stopped video was still
        leaving it (22 GB in use): Ollama either died of out-of-memory or quietly put
        the 27B on the CPU (0.5 of 18 GB on the GPU), and a 13 s plan took minutes.
        So: wait until the free memory is really there, and drop a planner copy that a
        previous load left mostly on the CPU so it loads again onto the card.
        """
        _activity(self.thread_id, "waiting for the GPU memory to clear")
        free = None
        for _ in range(45):
            try:
                free = ((await wis.health()).get("gpu") or {}).get("vram_free_mb")
            except Exception:                                  # noqa: BLE001
                free = None
            if free is None or free >= PLANNER_VRAM_MB:
                break
            await asyncio.sleep(2)
        else:
            log.warning("planner starting with only %s MB free", free)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                ps = (await client.get(f"{cfg.OLLAMA_URL}/api/ps")).json()
            for m in ps.get("models") or []:
                if cfg.PLANNER_MODEL in (m.get("name"), m.get("model")) and \
                        (m.get("size_vram") or 0) < 0.9 * (m.get("size") or 1):
                    log.info("planner was loaded mostly on the CPU; reloading it on the GPU")
                    await _unload(cfg.PLANNER_MODEL)
        except Exception as exc:                               # noqa: BLE001
            log.warning("could not check where the planner is loaded: %s", exc)

    async def renew(self) -> None:
        if self.lease:
            if not await wis.renew_gpu(self.lease, LEASE_HOLD_S):
                log.warning("planner lease %s ended early", self.lease)

    async def release(self) -> None:
        if not self.held:
            return
        self.held = False
        try:
            await asyncio.shield(_unload(cfg.PLANNER_MODEL))
            if self.lease:
                await asyncio.shield(wis.release_gpu(self.lease))
                _remember_lease(None)
        finally:
            self.lease = None
            gpuqueue.resume()


PLAN_SCHEMA_BASE = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "state": {"type": "string", "enum": ["continue", "wait", "done", "ask"]},
        "steps": {"type": "array", "items": {
            "type": "object",
            "properties": {"tool": {"type": "string"}, "why": {"type": "string"},
                           "args": {"type": "object"}},
            "required": ["tool", "why", "args"]}},
        "remember": {"type": "array", "items": {"type": "string"}},
        "review_in_s": {"type": "integer"},
    },
    "required": ["reply", "state", "steps"],
}


def _plan_schema() -> dict:
    schema = json.loads(json.dumps(PLAN_SCHEMA_BASE))
    schema["properties"]["steps"]["items"]["properties"]["tool"]["enum"] = list(tools())
    return schema


PLANNER_RULES = """You are the planner of Story Studio's assistant. You run on the studio's own \
GPU, and while you think every picture, video, speech and music job is paused -- so think \
efficiently and plan whole batches of work at once.

You never call tools yourself. You answer with ONE JSON object:
  reply   -- what to tell the person now (plain, short, friendly; may be "" while working).
             When something is finished, give its links (image_url, audio_url, video_url).
  steps   -- tool calls for the executors to run in order. Each: {"tool", "why", "args"}.
             Steps are numbered from #{first_step}. Use a result of an EARLIER step as
             "$<number>.<field>", e.g. "$12.image_id" or "$13.project_id".
             Give every argument you are sure of. You may leave an argument out and say in
             "why" what it must be (e.g. "narration = the story of $13, calm female voice"):
             a smaller model fills it in from the tool's schema.
  state   -- "continue": run the steps and call me straight back with the results (use it
               when the next decision depends on them, e.g. after list_projects).
             "wait": delegated work is running (renders, queued speech/music); I will be
               called back when it finishes or fails, and on a regular interval.
             "done": the request is complete and reported.
             "ask": you need the person to decide something; put the question in reply.
  remember -- durable preferences or corrections from the person ("always portrait",
             "Meera's voice for narration", "don't upscale"), phrased as a rule. Only things
             that should apply to future requests too. Usually [].
  review_in_s -- optional, when to check on running work (120-3600).

Self-correction: every step result comes back to you. If a step failed, READ the error and \
fix the cause with a corrected step (shorter scene seconds, another model, a valid size, a \
missing picture added first) rather than repeating the same call. If a finished picture or \
video frame does not match what was asked (you can see them), regenerate it with a better \
prompt. studio_status lists the person's recent failures with errors. After {max_fix} \
correction rounds without success, stop and explain to the person what went wrong.

Rules:
- The person's standing lessons below override defaults. Follow them.
- Never invent ids. Get them from results (list_projects, list_images, ...).
- Rendering is slow (about 2 minutes per 5 s of video). Start it, then state "wait".
- Don't poll with get_status/get_image steps yourself -- the watcher does that for you.
- People can attach pictures only on a project page; if one is needed, give them its page_url.
- A command from the person beats your plan: if they say stop, cancel or change something,
  do that.
"""


PRODUCTION_RULES = """
You are working INSIDE A PRODUCTION in the studio's editor, with the person, on their film.
The production's full state (story, references, scenes, shots with ids and timing, timeline
audio, mix) is given to you in every turn; the person sees the same thing on screen.
- "This shot" / "here" means the shot the person is looking at (marked in the state).
- THREE LEVELS. Each message from the person is tagged with the level it is about; work at that
  level and don't reach past it:
  LEVEL 1 · the whole story -> define (or revise) ALL the scenes and their shots.
  LEVEL 2 · a scene -> define the shots that scene should have (add, change, reorder, remove
            shots in THAT scene only; its references are its cast and set).
  LEVEL 3 · a shot -> define that specific shot: framing, length, make, prompt, references,
            picture (shot_picture_options), and its clip only if asked.
- WORK IN STAGES, in this order; finish one before the next and say in one line where you are:
  1. UNDERSTAND: read the whole story. Reply with a 3-5 line summary: the arc, the characters,
     the places. Ask only if something essential is missing.
  2. SCENES: identify ALL the scenes in one build_storyboard call: each with title, summary and
     refs = the characters and places in it (add them as refs). Shots may wait for stage 3.
     In the SAME breakdown, ALWAYS (the person's rule, 2026-11-25 -- clothes kept changing):
     a. a `prop` ref for each character's outfit (described exactly: garment, colour, fabric)
        and for each object a character keeps using, plus ONE `style` ref for the film's look;
     b. on every character and place ref, `with: [their usual outfit/objects and the style]`;
     c. on every scene and shot where an outfit or object changes, `wear: {character: [props]}`
        (and make those props too). Never leave clothes to each picture's imagination.
  3. CHARACTERS FIRST (the person's rule, 2026-11-25: "generation of image and media should
     happen when user has selected characters"): draw each character's picture (make_ref_picture
     or draw_pictures, which stops there), then STOP: show the person the characters and ask them
     to choose each look (✓ Use this look in References, a picture of their own, or telling you).
     Characters marked LOOK NOT CHOSEN get no shot picture, voice or video -- the studio refuses
     them anyway. When the person approves in words, call approve_looks.
  4. SCENE ASSETS, once the looks are chosen, scene by scene: draw_pictures (each shot's
     picture; shot_picture_options to offer choices for one shot); lines are voiced then.
  5. VIDEOS, a SEPARATE step: render_shots only when the person asks for videos, after they
     have seen the pictures. Drawing pictures never starts the video model.
  Pick up from the stage the production is at: look at the state before starting over.
- THE PERSON'S EDITS WIN. The state you are given is the truth: never undo or re-propose
  something the person changed. Scenes marked LOCKED-BY-THE-PERSON are not yours to change
  (nor their shots); work around them.
- THE SCRIPT'S LINES COME WITH THE SHOTS. When you lay out shots (build_storyboard), put every line
  of dialogue and narration from the story into the `dialogue` of the shot where it is said, word
  for word, with its speaker (a character's name, or "Narrator"). After the person applies, the
  lines are voiced automatically in each character's voice; shots where a character speaks on
  camera become lipsync, so their video is made from that voice.
- To add the script's lines to shots that already exist, call add_lines once with all of them
  (in story order). Use the story's own words exactly; write new lines only if the person asks.
  Do not ask permission to add lines the story already contains: the person asked for them.
- VOICES ARE MADE FIRST, VIDEO FROM THE VOICE (the person's rule). A shot that speaks: voice the
  line with add_dialogue(shot_id, ref_id, text) in the character's voice; it then generates that
  shot's video lip-synced to the voice. Never let a video model invent a voice: do not use
  `perform` for speech, and never rely on a clip's own sound.
- MIXING IS NOT RE-RENDERING. Music, ambience, levels, fades, timing and moving clips only change
  the mix (export_film mixes on the CPU in seconds): no render_shots for those.
- To lay out a story as scenes and shots use build_storyboard (nested scenes -> shots, plus
  characters as refs); for a long film call it once per 1-3 scenes so each answer stays short.
  Follow the story's OWN scene list (its headings, its order, its count): never invent, merge
  or rename scenes. Later calls ADD the next scenes; never redo scenes already in the state.
- THE WHOLE STORY IS IN THE STATE ("Story (complete, N characters)"). Read it all: its cast
  table, its scenes, every line. Never "reconstruct" dialogue or descriptions, and never say it
  is truncated unless the state says so.
- REFERENCES (characters, places) -- the person reviews these first, so get them right:
  * ONE reference per character, named exactly as the story names them. A name already in the
    references is that same reference: never add it again (refs may be named instead of ids).
  * Its description is the story's own description of that character, COMPLETE: age, build,
    skin, face, hair, costume and colours, emblems, manner. Copy it; do not shorten or invent.
  * A character shown at DIFFERENT AGES (newborn, child, teen, adult, old) gets one reference
    PER AGE ("Newborn Rāma", "Teenage Rāma"), each described only at that age, and each shot
    uses the age it shows. Do this for every such character at once, not one at a time.
  * When the story leaves out how a well-known figure looks (a deity, a historical person, an
    epic's hero, a period costume or weapon), web_search it and follow the traditional look
    (e.g. Viṣṇu is blue-skinned, not golden). Say in one line what you looked up.
  * The story's description ALWAYS wins. The state lists same-named characters from the
    person's other productions: reuse one (add_ref{from_ref_id, description}) only when its
    look agrees with this story; pass the story's description anyway. If it disagrees (age,
    skin, hair, costume), add a fresh reference instead.
  * make_ref_picture takes the reference's NAME (ref), so it works right after you add it.
  * WHAT GOES WITH A CHARACTER OR PLACE: an outfit that must stay the same, an object they
    always have, the film's look -- make each a reference (kind prop or style) and LINK it
    (`with: [names]` on the character/place in build_storyboard refs, or add_ref/edit_ref
    `links`). Linked props and style then come with that character into every shot, so the
    clothes do not change between scenes. WHEN IT CHANGES (another outfit in a scene, a prop
    picked up or put down in a shot): make that prop too and set `wear` on the scene
    (edit_scene{wear: {"Abhi": ["Abhi's kurta"]}}) or the shot (edit_shot{wear: {...}}), or give
    `wear` on scenes/shots in build_storyboard. A shot's wear beats its scene's, which beats the
    character's usual links; {"Abhi": null} goes back to the scene's / usual.
  * TO CHANGE PART OF A PICTURE (a shot's or a character's) use edit_picture with only what
    should change ("she is not smiling", "evening light"): it keeps everything else. Draw a
    picture again only when the person wants a different picture altogether.
  * A PICTURE THE PERSON GIVES FOR A CHARACTER is that character's look: set it at once with
    edit_ref{ref_id or name, image_id: "@F<n>"} (never draw a new one instead). A character
    sheet (several views side by side) is cut into its views automatically. Then redraw the
    pictures of the shots that character is in, and check the result says it was applied.
  * EVERY CHARACTER GETS ITS OWN VOICE, designed from a voice brief: give each character ref a
    `voice` (one sentence: gender, age, pitch, timbre, pace, accent the setting implies, how they
    usually sound -- e.g. "A woman in her fifties, warm low voice, soft Indian English accent,
    slow and kind"). It is designed automatically when the character is created and every line
    of theirs is cloned from it; never leave a speaking character on a preset of the wrong
    gender. design_voice redesigns one when the person asks for a different voice.
- When the person says to apply / approve / go ahead with your proposal(s), call apply_proposals
  (and then carry on with the next stage). Never apply on your own initiative.
- When the state says you may apply directly, your edits take effect at once: CARRY ON through
  the stages without asking for approval (the person can stop you or undo by asking). Ask only
  when something essential is genuinely unclear in the story.
- Other story, scene, shot, reference, audio-clip and mix EDITS go through propose_changes: the
  person reviews and applies them (unless the state says you may apply directly). One
  proposal per coherent idea, with a clear title; for a whole-story breakdown, one proposal
  that adds the scenes and shots (use `key`s so shots can name their new scene).
- Pictures for a shot: shot_picture_options (uses the shot's references so characters stay
  the same people). Lines of dialogue: add_dialogue with the character's ref_id. Score or
  ambience: add_music. These are made straight away.
- Generate video clips (render_shots) only when the person asks for videos; each shot
  needs a picture and a prompt that says what moves and how the camera moves. A "perform"
  shot uses the audio under it that is marked use_in_video, and its reference pictures.
- To decide whether a track's audio is used for video generation, set use_in_video on its
  clips (edit_clip). Mixing: set_mix / edit_clip (gains in dB, fades, ducking).
- When laying out shots, mark each shot that is ONE CONTINUOUS TAKE with the shot before it
  (`continues: true` in build_storyboard): it opens on that shot's last frame and gets no picture
  of its own. Cuts (new angle, framing, place, moment) are `continues: false`.
- Continuity between shots: `start_from: "prev_last"` makes a shot open on the previous shot's
  last frame (taken when it renders: render the two together); `use_frame` puts any frame of a
  rendered clip (e.g. at: "last") into a shot as its picture, or into a new shot after it.
- For every new or changed shot, fill `framing` (EWS|WS|MS|MCU|CU|ECU|OTS|POV|2S|INSERT) and
  `make` (animate|perform|lipsync|motion|longtalk|talk2|recast|restyle) explicitly; a close-up is framing "CU", not just a
  word in the prompt. A shot where a character speaks on camera is `lipsync` (voiced first,
  video from the voice); `perform` only when the person asks for it. `motion` = the person in
  the shot's picture copies a reference video's movements exactly (a dance, a gesture): set
  make "motion" and add the video with add_reference_video. `longtalk` = one person talks or
  sings for up to a minute; `talk2` = two people in the picture talk (shot opts.speakers: a
  timeline clip each); `recast` = characters replace the people in a video (opts.cast:
  [{ref, who}]); `restyle` = change a video's look (opts.change). Their settings go in the
  edit_shot `opts` object.
- USE CASES (the person's design: one model per use case, listed upfront in the Shot tab as
  "What happens in this shot?"). Pick the shot's `make` from what should happen:
  animate  "Bring the picture to life" - MiniMax H3 i2v; starts exactly on the shot picture.
  perform  "Keep characters true to their references" - MiniMax H3 ref2v; shot picture
           optional (the characters'/places' pictures and reference videos guide it).
  lipsync  "Someone speaks or sings a line" (<=30 s) - Wan2.2-S2V from the line under the shot.
  longtalk "Talk or sing for longer" (<=60 s) - InfiniteTalk; opts.speakers=[{clip}] (a
           dialogue or music clip on the timeline).
  talk2    "Two people talk" - InfiniteTalk; picture with both people; opts.speakers =
           [{clip},{clip}] spoken one after the other; opts.layout left_right|right_left.
  motion   "Copy a dance or movement from a video" - Wan Animate 2; one reference video;
           no picture needed (the character in the shot performs, from their own picture).
  recast   "Recast the people in a video" - Viggle Remake; one reference video + opts.cast
           [{ref, who}] (up to 3); no picture needed (the painted frame is made from the video).
  restyle  "Change a video's look" - Viggle Remake edit; one reference video + opts.change.
  Shots marked no-picture-needed must NOT get draw_pictures/picture work just to render.
  Voices for talk shots are clips on the timeline: use existing clip_ids, or add_dialogue
  first; only the person can upload a file. Recast faces come out right when the character
  has a face close-up among their views or own photos.
- Only use project_id from the state. Keep replies short; the editor shows the details.
"""


def _system_prompt(user_id: str, first_step: int, production: bool = False,
                   project_id: str | None = None) -> str:
    rules = PLANNER_RULES.replace("{first_step}", str(first_step)) \
                         .replace("{max_fix}", str(cfg.AGENT_MAX_CORRECTIONS))
    if production:
        rules += PRODUCTION_RULES
    # In a production only the person's general lessons and this story's own apply; the
    # general assistant sees them all, each story's marked as such.
    rows = lessons(user_id, project_id, everything=not production)
    lessons_txt = "\n".join(f"- {_lesson_text(r)}" for r in rows) or "- (none yet)"
    if production:
        lessons_txt += ("\n(Looks, style, costume and language come from THIS story, its "
                        "references and the person's words; never carry them over from "
                        "another story.)")
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (f"{rules}\nNow: {today}.\n\nThe person's standing lessons:\n{lessons_txt}\n\n"
            f"About the studio:\n{PRODUCTION_ABOUT if production else mcp.INSTRUCTIONS}\n\n"
            f"Tools (* = required):\n"
            f"{catalogue(production)}")


def _digest(obj, limit: int = 700) -> str:
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else text[:limit] + "...}"


MAX_EYES = 6      # pictures per planner call (attachments, pinned items, results)


def _context_items(row) -> list[dict]:
    try:
        return json.loads(row["context"] or "[]")
    except (ValueError, IndexError, KeyError):
        return []


def _library_item(user_id: str, kind: str, item_id: str) -> dict | None:
    """A studio item the person owns, described for the planner and the page."""
    if kind == "image":
        r = db.one("SELECT id, prompt, width, height, path, status FROM images WHERE id=? AND "
                   "user_id=?", (item_id, user_id))
        if r is None or r["status"] != "succeeded":
            return None
        return {"kind": kind, "id": r["id"], "title": (r["prompt"] or "")[:120],
                "detail": f"picture image_id={r['id']} {r['width']}x{r['height']}, "
                          f"prompt: {(r['prompt'] or '')[:300]}", "path": r["path"]}
    if kind == "speech":
        r = db.one("SELECT id, text, voice, duration_s, status FROM speech WHERE id=? AND "
                   "user_id=?", (item_id, user_id))
        if r is None:
            return None
        return {"kind": kind, "id": r["id"], "title": (r["text"] or "")[:120],
                "detail": f"speech clip speech_id={r['id']} ({r['voice']}, {r['status']}), "
                          f"text: {(r['text'] or '')[:600]}"}
    if kind == "music":
        r = db.one("SELECT * FROM music WHERE id=? AND user_id=?", (item_id, user_id))
        if r is None:
            return None
        try:
            p = json.loads(r["params"] or "{}")
        except ValueError:
            p = {}
        desc = str(p.get("description") or p.get("style") or "")
        lyr = str(p.get("lyrics") or "")
        return {"kind": kind, "id": r["id"], "title": (r["title"] or desc or "song")[:120],
                "detail": f"song music_id={r['id']} \"{r['title']}\" ({r['task']}, {r['status']}, "
                          f"{round(r['duration_s'] or 0)} s): {desc[:300]}"
                          + (f"; lyrics: {lyr[:600]}" if lyr else "")}
    if kind == "project":
        r = db.one("SELECT id, title, mode, story, status FROM projects WHERE id=? AND user_id=?",
                   (item_id, user_id))
        if r is None:
            return None
        return {"kind": kind, "id": r["id"], "title": r["title"],
                "detail": f"video project project_id={r['id']} \"{r['title']}\" ({r['mode']}, "
                          f"{r['status']}), story: {(r['story'] or '')[:600]}"}
    return None


def _history(thread_id: str) -> tuple[list[dict], int]:
    """The conversation as the planner sees it: the person's words (with what they
    attached), its own replies, and the studio log folded into user turns. Pictures
    the person attached recently are put in front of its eyes. Returns (messages,
    pictures used)."""
    rows = db.query("SELECT * FROM agent_messages WHERE thread_id=? ORDER BY seq DESC LIMIT 50",
                    (thread_id,))[::-1]
    files = {f["id"]: f for f in agentfiles.files_of(thread_id)}
    msgs: list[dict] = []
    log_lines: list[str] = []
    eyes = 0
    user_rows = [r for r in rows if r["role"] == "user"]
    recent_users = {r["seq"] for r in user_rows[-3:]}

    def flush():
        if log_lines:
            msgs.append({"role": "user", "content": "[studio log]\n" + "\n".join(log_lines)})
            log_lines.clear()

    for r in rows:
        meta = json.loads(r["meta"] or "{}")
        if r["role"] == "user":
            flush()
            msg = {"role": "user", "content": r["content"] or "(sent files)"}
            if meta.get("focus"):
                f = meta["focus"]
                lvl = f.get("level") or ("shot" if f.get("shot_id") else "story")
                where = {"story": "LEVEL 1 · the whole story",
                         "scene": f"LEVEL 2 · scene {f.get('label')} (scene_id={f.get('scene_id')})",
                         "shot": f"LEVEL 3 · shot {f.get('label')} (shot_id={f.get('shot_id')})"
                         }.get(lvl, lvl)
                msg["content"] = f"({where}) " + msg["content"]
            attached = [files[f] for f in meta.get("files") or [] if f in files]
            if attached:
                recent = r["seq"] in recent_users
                msg["content"] += "\n\n[attached files]\n" + "\n".join(
                    agentfiles.describe(f, agentfiles.EXCERPT if recent else 600)
                    for f in attached)
                if recent:
                    pics = [p for f in attached for p in agentfiles.pictures(f)][:MAX_EYES - eyes]
                    if pics:
                        msg["images"] = [base64.b64encode(p).decode() for p in pics]
                        eyes += len(pics)
            msgs.append(msg)
        elif r["role"] == "assistant":
            flush()
            msgs.append({"role": "assistant", "content": r["content"] or "(working)"})
        elif r["role"] == "step":
            res = meta.get("result")
            log_lines.append(f"#{meta.get('n')} {meta.get('tool')} [{meta.get('status')}] "
                             f"args={_digest(meta.get('args'), 250)} -> {_digest(res, 500)}")
        elif r["role"] == "result":
            log_lines.append(f"(watcher) {r['content']} -> {_digest(meta.get('result'), 500)}")
        elif r["role"] in ("note", "error"):
            log_lines.append(f"({r['content']})")
    flush()
    return msgs, eyes


def _pinned(thread) -> tuple[str, list[bytes]]:
    """Library items the person put in this conversation's context."""
    lines, pics = [], []
    for c in _context_items(thread):
        item = _library_item(thread["user_id"], c.get("kind"), c.get("id"))
        if item is None:
            continue
        lines.append("- " + item["detail"])
        if item.get("path") and Path(item["path"]).exists():
            from app import images
            try:
                pics.append(images.preview_jpeg(item["path"]))
            except Exception:                                  # noqa: BLE001
                pass
    if not lines:
        return "", []
    return "In context (the person pinned these from their library):\n" + "\n".join(lines), pics


def _next_step_number(thread_id: str) -> int:
    row = db.one("SELECT COUNT(*) AS n FROM agent_messages WHERE thread_id=? AND role='step'",
                 (thread_id,))
    return (row["n"] if row else 0) + 1


def _step_results(thread_id: str) -> dict[int, dict]:
    out = {}
    for r in db.query("SELECT meta FROM agent_messages WHERE thread_id=? AND role='step'",
                      (thread_id,)):
        meta = json.loads(r["meta"] or "{}")
        if meta.get("n") and isinstance(meta.get("result"), dict):
            out[int(meta["n"])] = meta["result"]
    return out


def _parse_json(text: str) -> dict | None:
    text = (text or "").strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if m:
        text = m.group(1)
    try:
        val = json.loads(text)
        return val if isinstance(val, dict) else None
    except ValueError:
        a, b = text.find("{"), text.rfind("}")
        if a != -1 and b > a:
            try:
                val = json.loads(text[a:b + 1])
                return val if isinstance(val, dict) else None
            except ValueError:
                return None
    return None


def _last_user_seq(thread_id: str) -> int:
    row = db.one("SELECT MAX(seq) AS s FROM agent_messages WHERE thread_id=? AND role='user'",
                 (thread_id,))
    return (row["s"] or 0) if row else 0


_seen: dict[str, int] = {}


async def _stream_plan(thread_id: str, payload: dict) -> dict:
    """Call the planner streaming, so the page can show its thinking live."""
    thinking, content, last, final = [], [], 0.0, {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=20.0)) as client:
        async with client.stream("POST", f"{cfg.OLLAMA_URL}/api/chat",
                                 json={**payload, "stream": True}) as resp:
            if resp.status_code >= 400:
                raise RuntimeError(f"ollama: {resp.status_code} {(await resp.aread())[:300]!r}")
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                chunk = json.loads(line)
                msg = chunk.get("message") or {}
                if msg.get("thinking"):
                    thinking.append(msg["thinking"])
                if msg.get("content"):
                    content.append(msg["content"])
                now = time.monotonic()
                if now - last > 0.8:
                    last = now
                    thought = "".join(thinking)[-400:] if not content else "writing the plan…"
                    _set(thread_id, thought=thought)
                if chunk.get("done"):
                    final = chunk
    final["content"] = "".join(content)
    return final


async def _plan(thread, trigger: str, turn: GpuTurn) -> dict:
    _seen[thread["id"]] = _last_user_seq(thread["id"])
    first = _next_step_number(thread["id"])
    pid = thread["project_id"] if "project_id" in thread.keys() else None
    system = {"role": "system", "content": _system_prompt(thread["user_id"], first, bool(pid), pid)}
    history, eyes = _history(thread["id"])
    pinned, pinned_pics = _pinned(thread)
    files = agentfiles.files_of(thread["id"])
    files_line = ("Files in this conversation: " + ", ".join(
        f"F{f['num']} {f['name']} ({f['kind']})" for f in files) +
        '. In a tool argument, "@F<n>" stands for a file (bytes for *_base64, the image_id '
        "for pictures, the text for text).") if files else ""
    focus = _focus_meta(thread["id"])
    watch = _watch_summary(thread["id"])

    def build(level: int) -> list[dict]:
        """The prompt at compaction `level` (0 = everything as is). The stable parts come
        first (system, then history) so Ollama can reuse its cache across rounds."""
        hist = compact_history(history, level)
        production_txt = ""
        if pid:
            production_txt = "[the production now]\n" + editor.describe_for_agent(
                pid, focus.get("shot_id") or None, compact=max(0, level - 1),
                focus_scene=focus.get("scene_id") or None)
        fin = {"role": "user", "content": "\n".join(x for x in (
            f"[{trigger}]", production_txt, pinned, files_line, watch,
            "Answer with the JSON object.") if x)}
        return [system] + hist + [fin]

    # Compact only as far as needed to fit the fast window (measured 2026-11-25: 40960 runs
    # fully on the GPU at 116 tok/s; 65536 spills to the CPU at ~35 tok/s).
    think = think_budget(pid)
    fast = PLANNER_WINDOWS[0] - think - 1024
    for level in range(COMPACT_LEVELS + 1):
        messages = build(level)
        if _est_tokens(messages) <= fast:
            break
    final = messages[-1]
    pics = pinned_pics[:max(0, MAX_EYES - eyes)]
    views = _views.pop(thread["id"], [])[:max(0, MAX_EYES - eyes - len(pics))]
    if views:
        final["content"] += "\nAlso attached: " + "; ".join(label for label, _ in views) + \
                            ". Check they match what was asked."
    if pics or views:
        final["images"] = [base64.b64encode(p).decode() for p in pics] + \
                          [base64.b64encode(jpg).decode() for _, jpg in views]
    messages, num_ctx, est = _fit_window(messages, think)
    log.info("planner %s: prompt ~%d tokens, compaction %d/%d, think %d, window %d",
             thread["id"], est, level, COMPACT_LEVELS, think, num_ctx)
    _activity(thread["id"], "planning", "planning")
    for attempt in range(3):
        await turn.renew()
        t0 = time.monotonic()
        result = await _stream_plan(thread["id"], {
            "model": cfg.PLANNER_MODEL, "messages": messages,
            "think": True, "format": _plan_schema(), "keep_alive": "10m",
            "options": {"num_ctx": num_ctx, "num_predict": think,
                        "temperature": 0.6 if attempt == 0 else 0.8}})
        out = _parse_json(result.get("content", ""))
        log.info("planner %s: %.1fs, %s tokens, ok=%s%s", thread["id"], time.monotonic() - t0,
                 result.get("eval_count"), out is not None,
                 " (cut off: out of room to think)" if result.get("done_reason") == "length" else "")
        if out is None and result.get("done_reason") == "length" and think < PLANNER_THINK_MAX:
            think = min(PLANNER_THINK_MAX, think * 2)
            messages, num_ctx, est = _fit_window(messages, think)
            log.info("planner %s: retrying with think %d, window %d", thread["id"], think, num_ctx)
        if out is not None and isinstance(out.get("steps", []), list):
            out.setdefault("reply", "")
            out.setdefault("state", "done")
            _set(thread["id"], thought="")
            return out
    raise RuntimeError("the planner did not return a usable plan")


EXECUTOR_RULES = """You are an executor in Story Studio. Make exactly ONE call to the tool you \
are given, with arguments that satisfy its schema. Use exact ids and values from the earlier \
results; never invent ids. If the draft arguments are fine, repeat them. If a previous attempt \
was refused, read the error and change what caused it. If the step is impossible, answer in one \
sentence why, without calling the tool."""


async def _executor_fix(thread_id: str, step: dict, schema: dict, draft: dict,
                        problem: str, results: dict, turn: "GpuTurn | None" = None
                        ) -> tuple[dict | None, str]:
    """A corrected call: by the planner itself on the GPU while this turn still holds it
    (2026-11-25: a set_scenes repair took ~35 s on the CPU model while the 27B had just been
    unloaded), otherwise by the small CPU model. Returns (args or None, model used)."""
    on_gpu = bool(turn and turn.held)
    model = cfg.PLANNER_MODEL if on_gpu else cfg.EXECUTOR_MODEL
    name = step["tool"]
    earlier = "\n".join(f"#{n}: {_digest(r, 500)}" for n, r in sorted(results.items())[-8:])
    files = ", ".join(f"F{f['num']}={f['name']} ({f['kind']})"
                      for f in agentfiles.files_of(thread_id))
    prompt = (("Attached files (pass \"@F<n>\" to use one): " + files + "\n" if files else "") +
              f"Step: {step.get('why', '')}\nTool: {name}\nDraft arguments: "
              f"{json.dumps(draft, ensure_ascii=False)}\nProblem: {problem}\n\n"
              f"Earlier results:\n{earlier or '(none)'}")
    _activity(thread_id, f"{'planner' if on_gpu else 'executor'} ({model}, "
                         f"{'GPU' if on_gpu else 'CPU'}) is working out {name}")
    options = ({"num_ctx": 16384, "temperature": 0.2} if on_gpu else
               {"num_gpu": 0, "num_thread": cfg.EXECUTOR_THREADS,
                "num_ctx": cfg.EXECUTOR_CTX, "temperature": 0.2})
    try:
        if on_gpu:
            await turn.renew()
        result = await _ollama("/api/chat", {
            "model": model, "stream": False, "think": False,
            "keep_alive": "10m" if on_gpu else "5m",
            "messages": [{"role": "system", "content": EXECUTOR_RULES},
                         {"role": "user", "content": prompt}],
            "tools": [{"type": "function", "function": {
                "name": name, "description": tools()[name]["description"][:1500],
                "parameters": schema}}],
            "options": options}, timeout=600)
    except Exception as exc:                                   # noqa: BLE001
        log.warning("executor (%s) failed: %s", model, exc)
        return None, model
    for call in (result.get("message") or {}).get("tool_calls") or []:
        fn = call.get("function") or {}
        if fn.get("name") == name:
            args = fn.get("arguments")
            if isinstance(args, str):
                args = _parse_json(args)
            if isinstance(args, dict):
                return args, model
    return None, model


def _watch_key(name: str, args: dict, result: dict) -> tuple[str, str] | None:
    status = str(result.get("status") or "")
    if status in TERMINAL:
        return None
    if name in ("render_video", "get_status", "render_shots") and result.get("project_id"):
        return ("project", result["project_id"]) if status in ("queued", "running", "") \
            else None
    kind = CREATES.get(name)
    if kind and result.get(WATCH[kind][1]):
        return kind, result[WATCH[kind][1]]
    if name in ("add_dialogue", "add_music") and result.get("clip_id"):
        return "clip", result["clip_id"]
    if name == "retry_job" and result.get("id"):
        return {"voice_job": "voice_job"}.get(result.get("kind"), result.get("kind")), result["id"]
    return None


def _media(result: dict) -> list[dict]:
    out = []
    if result.get("image_url") and result.get("image_id"):
        out.append({"type": "image", "kind": "image", "id": result["image_id"]})
    if result.get("audio_url"):
        if result.get("speech_id"):
            out.append({"type": "audio", "kind": "speech", "id": result["speech_id"]})
        elif result.get("music_id"):
            out.append({"type": "audio", "kind": "music", "id": result["music_id"]})
        else:
            out.append({"type": "audio", "url": result["audio_url"]})
    if result.get("video_url") and result.get("project_id"):
        out.append({"type": "video", "kind": "video", "id": result["project_id"]})
    for k in ("result_url", "song_url"):
        if result.get(k):
            out.append({"type": "audio", "url": result[k]})
    return out


def _video_frame(project_id: str) -> bytes | None:
    row = db.one("SELECT output_path FROM projects WHERE id=?", (project_id,))
    if not row or not row["output_path"] or not Path(row["output_path"]).exists():
        return None
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", "2", "-i", row["output_path"], "-frames:v", "1",
             "-vf", "scale=640:-2", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
            capture_output=True, timeout=30)
        return proc.stdout or None
    except Exception:                                          # noqa: BLE001
        return None


def _see(thread_id: str, kind: str, item_id: str, preview: bytes | None = None) -> None:
    """Queue a picture of a finished result for the planner's next look."""
    if preview is None and kind == "image":
        row = db.one("SELECT path FROM images WHERE id=?", (item_id,))
        if row and row["path"] and Path(row["path"]).exists():
            from app import images
            try:
                preview = images.preview_jpeg(row["path"])
            except Exception:                                  # noqa: BLE001
                preview = None
    if preview is None and kind == "project":
        preview = _video_frame(item_id)
    if preview:
        label = f"picture {item_id}" if kind == "image" else f"a frame of video {item_id}"
        _views[thread_id].append((label, preview))


async def _execute(thread, user, steps: list[dict], plan_seq: int = 0,
                   turn: "GpuTurn | None" = None) -> tuple[int, bool]:
    """Run planned steps. Returns (failed_count, produced_results_to_review).
    The GPU turn is handed back just before the first HEAVY step (they queue in WIS behind
    the planner's own lease otherwise); until then a step that needs fixing is fixed by the
    planner on the GPU."""
    tid = thread["id"]
    results = _step_results(tid)
    tracked = _tracked(_thread(tid))
    failed, produced = 0, False
    _set(tid, status="executing")
    for step in steps:
        name = step.get("tool")
        n = _next_step_number(tid)
        if name not in tools():
            _post(tid, "step", step.get("why", ""), n=n, tool=name, status="failed", plan=plan_seq,
                  result={"error": f"there is no tool called {name}"})
            failed += 1
            continue
        if name in HEAVY and turn is not None and turn.held:
            _activity(tid, "handing the GPU back for the work")
            await turn.release()
        schema = tools()[name]["schema"]
        draft = _resolve(step.get("args") or {}, results)
        if tools()[name].get("editor") and thread["project_id"] and not draft.get("project_id"):
            draft["project_id"] = thread["project_id"]
        seq = _post(tid, "step", step.get("why", ""), n=n, tool=name, args=draft,
                    status="running", plan=plan_seq)
        _activity(tid, f"step {steps.index(step) + 1} of {len(steps)}: {step.get('why') or name}"[:160],
                  "executing")
        args, problems = _validate(draft, schema)
        ok, result, preview, fixed_by = False, {"error": "not run"}, None, None
        for attempt in range(EXECUTOR_TRIES):
            if problems and len(json.dumps(args, ensure_ascii=False)) > 4000:
                # A big call (a whole storyboard) is not the small CPU model's job: at
                # ~12 tokens/s rebuilding it takes many minutes (it stalled a 47-change
                # proposal on 2026-11-24). The planner gets the error and fixes it.
                result = {"error": "; ".join(problems)[:1500]}
                break
            if problems:
                fixed, model = await _executor_fix(tid, step, schema, args, "; ".join(problems),
                                                   results, turn)
                if fixed is None:
                    result = {"error": "the executor could not build a valid call: "
                                       + "; ".join(problems)}
                    break
                fixed_by = model
                args, problems = _validate(_resolve(fixed, results), schema)
                if problems:
                    continue
            _patch(seq, args=args, status="running", attempt=attempt + 1)
            ok, result, preview = await _run_tool(user, name, args, thread["base_url"], tid)
            if ok and not result.get("error"):
                break
            ok = False
            problems = [f"the studio refused the call: {result.get('error', 'failed')}"]
            if attempt + 1 < EXECUTOR_TRIES:
                _patch(seq, status="fixing", result=result)
        status = "ok" if ok else "failed"
        if ok and fixed_by:
            status = "fixed"
        media = _media(result) if ok else []
        _patch(seq, args=args, status=status, result=result, media=media,
               executor=fixed_by or "direct")
        results[n] = result
        if not ok:
            failed += 1
            continue
        if media:
            for m in media:
                if m.get("kind") in ("image", "video"):
                    produced = True
                    _see(tid, "image" if m["kind"] == "image" else "project", m["id"],
                         preview if m["kind"] == "image" else None)
        key = _watch_key(name, args, result)
        if key and not any(t["kind"] == key[0] and t["id"] == key[1] for t in tracked):
            tracked.append({"kind": key[0], "id": key[1], "label": f"#{n} {name}",
                            "status": str(result.get("status") or "queued"), "seen": False})
    _set(tid, tracked=json.dumps(tracked))
    return failed, produced


def _clip_status(clip_id: str) -> dict:
    row = db.one("SELECT project_id FROM prod_clips WHERE id=?", (clip_id,))
    if row is None:
        return {"status": "failed", "error": "the clip was removed"}
    editor.refresh_clips(row["project_id"])
    c = db.one("SELECT status, error, name FROM prod_clips WHERE id=?", (clip_id,))
    status = {"ready": "succeeded", "pending": "running"}.get(c["status"], c["status"])
    return {"clip_id": clip_id, "status": status, "name": c["name"],
            **({"error": c["error"]} if c["error"] else {})}


def _focus_meta(thread_id: str) -> dict:
    row = db.one("SELECT meta FROM agent_messages WHERE thread_id=? AND role='user' "
                 "ORDER BY seq DESC LIMIT 1", (thread_id,))
    f = (json.loads(row["meta"] or "{}").get("focus") or {}) if row else {}
    return f if isinstance(f, dict) else {}


def _focus(thread_id: str) -> str | None:
    row = db.one("SELECT meta FROM agent_messages WHERE thread_id=? AND role='user' "
                 "ORDER BY seq DESC LIMIT 1", (thread_id,))
    f = (json.loads(row["meta"] or "{}").get("focus") or {}) if row else {}
    return f.get("shot_id") or None


def _watch_summary(thread_id: str) -> str:
    row = _thread(thread_id)
    tracked = _tracked(row)
    if not tracked:
        return "Delegated work: none running."
    lines = []
    for t in tracked:
        extra = f" error={t['error']}" if t.get("error") else ""
        prog = f" progress={t['progress']}" if t.get("progress") is not None else ""
        lines.append(f"- {t['label']}: {t['kind']} {t['id']} is {t['status']}{prog}{extra}")
    return "Delegated work:\n" + "\n".join(lines)


def _allowed(user: auth.User) -> bool:
    if user.is_admin or auth.is_unlimited(user.id):
        return True
    q = _turns[user.id]
    while q and q[0] < time.time() - 3600:
        q.popleft()
    return len(q) < cfg.USER_AGENT_TURNS_PER_HOUR


async def _turn(thread_id: str) -> None:
    thread = _thread(thread_id)
    user = load_user(thread["user_id"])
    if user is None:
        _set(thread_id, status="stopped", activity="account no longer active")
        return
    if not _allowed(user):
        _post(thread_id, "error", f"Planner limit reached ({cfg.USER_AGENT_TURNS_PER_HOUR} "
                                  "turns an hour). Try again later.")
        _set(thread_id, status="idle", activity="")
        return
    _turns[user.id].append(time.time())
    trigger = thread["reason"] or "new message from the person"
    if db.one("SELECT 1 FROM agent_files WHERE thread_id=? AND status='processing'", (thread_id,)):
        _activity(thread_id, "reading your files (transcribing audio, extracting text)", "queued")
        await agentfiles.wait_ready(thread_id)
    turn = GpuTurn(thread_id)
    final_state = "done"
    try:
        for rnd in range(MAX_ROUNDS):
            thread = _thread(thread_id)
            if not turn.held:
                await turn.acquire()
            out = await _plan(thread, trigger, turn)
            for text in out.get("remember") or []:
                # Said while working on a story: it is about that story.
                _add_lesson(user.id, text, thread["project_id"])
            if (out.get("reply") or "").strip():
                _post(thread_id, "assistant", out["reply"].strip(), state=out.get("state"))
            steps = [s for s in out.get("steps") or [] if isinstance(s, dict)]
            final_state = out.get("state", "done")
            if out.get("review_in_s"):
                _set(thread_id, next_review_at=_now() + max(120, min(3600,
                                                                     int(out["review_in_s"]))))
            if not steps:
                break
            plan_seq = _post(thread_id, "plan", "", steps=[
                {"tool": s.get("tool"), "why": s.get("why", "")} for s in steps])
            failed, produced = await _execute(thread, user, steps, plan_seq, turn)
            if failed:
                fixes = (_thread(thread_id)["corrections"] or 0) + 1
                _set(thread_id, corrections=fixes)
                if fixes > cfg.AGENT_MAX_CORRECTIONS:
                    _post(thread_id, "note", f"{failed} step(s) failed and the correction "
                                             "budget is used up; reporting back.")
                    trigger = ("correction budget used up: explain to the person what failed "
                               "and what they can do; state ask")
                else:
                    trigger = f"{failed} step(s) failed -- read the errors and correct"
                if fixes > cfg.AGENT_MAX_CORRECTIONS + 1:
                    break
                continue
            if final_state == "continue":
                trigger = "results of your steps are in the log"
                continue
            pending = [t for t in _tracked(_thread(thread_id)) if t["status"] not in TERMINAL]
            if produced and not pending:
                # Finished within the call (a picture, a clip): look at it before telling
                # the person it is done.
                trigger = "review: new results are ready -- check them, then report or correct"
                continue
            break
        else:
            _post(thread_id, "note", f"Stopped after {MAX_ROUNDS} planning rounds.")
    except asyncio.CancelledError:
        _post(thread_id, "note", "Stopped.")
        raise
    except Exception as exc:                                   # noqa: BLE001
        log.exception("agent turn %s failed", thread_id)
        _post(thread_id, "error", f"The assistant hit a problem: {exc}"[:400])
    finally:
        await turn.release()
        row = _thread(thread_id)
        if row is not None and row["status"] != "stopped" and \
                _last_user_seq(thread_id) > _seen.pop(thread_id, 0):
            _queue_turn(thread_id, "the person sent a new message while you worked; follow it")
        elif row is not None and row["status"] != "stopped":
            pending = [t for t in _tracked(row) if t["status"] not in TERMINAL]
            if pending:
                nxt = row["next_review_at"] if row["next_review_at"] and \
                    row["next_review_at"] > _now() else _now() + cfg.AGENT_REVIEW_S
                _set(thread_id, status="watching", reason="", next_review_at=nxt, stage_at=_now(),
                     thought="",
                     activity=f"watching {len(pending)} job(s); planner reviews when one "
                              "finishes, or at the next check-in")
            else:
                _set(thread_id, status="idle", reason="", activity="", thought="",
                     **({"corrections": 0} if final_state in ("done", "ask") else {}))


# =========================================================================== loops

def _lease_file():
    return cfg.DATA_DIR / "planner_lease.txt"


def _remember_lease(lease_id: str | None) -> None:
    try:
        if lease_id:
            _lease_file().write_text(lease_id)
        else:
            _lease_file().unlink(missing_ok=True)
    except OSError:
        pass


async def release_leftover_lease() -> None:
    """A restart mid-turn leaves the planner's lease held in WIS with nothing using the
    card; hand it back before the worker starts."""
    f = _lease_file()
    if f.exists():
        lease = f.read_text().strip()
        if lease:
            log.info("returning the planner's GPU lease %s left by the previous process", lease)
            await wis.release_gpu(lease)
        _remember_lease(None)


async def worker() -> None:
    """One planner at a time: there is one GPU and one big model."""
    await release_leftover_lease()
    db.run("UPDATE agent_threads SET status='queued', reason='resumed after a restart' "
           "WHERE status IN ('planning','executing','queued')")
    while True:
        try:
            row = db.one("SELECT id FROM agent_threads WHERE status='queued' "
                         "ORDER BY updated_at LIMIT 1")
            if row is None:
                _wake.clear()
                try:
                    await asyncio.wait_for(_wake.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                continue
            task = asyncio.create_task(_turn(row["id"]))
            _active[row["id"]] = task
            try:
                await task
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
            finally:
                _active.pop(row["id"], None)
        except asyncio.CancelledError:
            raise
        except Exception:                                      # noqa: BLE001
            log.exception("agent worker survived an error")
            await asyncio.sleep(5)


async def _poll(thread) -> None:
    user = load_user(thread["user_id"])
    if user is None:
        return
    tracked, finished = _tracked(thread), []
    for t in tracked:
        if t["status"] in TERMINAL:
            continue
        if t["kind"] == "clip":
            ok, result, preview = True, _clip_status(t["id"]), None
            tool = "timeline"
        else:
            tool, key = WATCH[t["kind"]]
            ok, result, preview = await _run_tool(user, tool, {key: t["id"]}, thread["base_url"])
        if not ok:
            t["status"], t["error"] = "failed", result.get("error")
        else:
            t["status"] = str(result.get("status") or t["status"])
            t["progress"] = result.get("progress")
            if result.get("error"):
                t["error"] = str(result["error"])[:300]
        if t["status"] in TERMINAL:
            finished.append(t)
            media = _media(result) if ok else []
            _post(thread["id"], "result" if media else "note",
                  f"{t['label']} {t['kind']} {t['status']}" +
                  (f": {t.get('error')}" if t.get("error") else ""),
                  **({"n": None, "tool": tool, "status": "ok" if t["status"] == "succeeded"
                      else "failed", "result": result, "media": media, "watched": True}
                     if media else {}))
            if t["status"] == "succeeded":
                _see(thread["id"], t["kind"], t["id"], preview)
    _set(thread["id"], tracked=json.dumps(tracked))
    if finished:
        names = ", ".join(f"{t['label']} {t['status']}" for t in finished)
        _queue_turn(thread["id"], f"review: delegated work finished ({names}) -- check the "
                                  "results; report them or correct")
    elif thread["next_review_at"] and thread["next_review_at"] <= _now():
        _queue_turn(thread["id"], "regular check-in: work is still running; correct anything "
                                  "that looks stuck or wrong, otherwise state wait")


async def monitor() -> None:
    while True:
        try:
            for row in db.query("SELECT * FROM agent_threads WHERE status='watching'"):
                await _poll(row)
        except asyncio.CancelledError:
            raise
        except Exception:                                      # noqa: BLE001
            log.exception("agent monitor survived an error")
        await asyncio.sleep(cfg.AGENT_POLL_S)


# =========================================================================== commands

def _status_text(row) -> str:
    lines = [f"Status: {row['status']}" + (f" — {row['activity']}" if row["activity"] else "")]
    tracked = _tracked(row)
    for t in tracked:
        lines.append(f"• {t['label']}: {t['kind']} {t['status']}"
                     + (f" ({t['progress']}%)" if t.get("progress") not in (None, "") else "")
                     + (f" — {t['error']}" if t.get("error") else ""))
    if row["next_review_at"] and row["status"] == "watching":
        mins = max(0, int((row["next_review_at"] - _now()) // 60))
        lines.append(f"Next planner check-in in about {mins} min (sooner if something finishes).")
    lines.append(f"Planner: {cfg.PLANNER_MODEL} (GPU) · executor: {cfg.EXECUTOR_MODEL} (CPU) · "
                 f"renders waiting: {render.queue_depth()}")
    return "\n".join(lines)


def stop(thread_id: str) -> None:
    _set(thread_id, status="stopped", activity="stopped", reason="")
    task = _active.get(thread_id)
    if task is not None:
        task.cancel()


def _command(user, thread, text: str) -> str | None:
    """Slash commands answered by code, without waking the planner."""
    cmd, _, rest = text.strip().partition(" ")
    cmd = cmd.lower()
    if cmd == "/stop":
        stop(thread["id"])
        return "Stopped. Running renders keep going; say “cancel the render” to stop them too."
    if cmd == "/status":
        return _status_text(_thread(thread["id"]))
    if cmd == "/review":
        _queue_turn(thread["id"], "the person asked for a review now: check everything "
                                  "running or finished and correct what needs it")
        return "Asking the planner to review now."
    if cmd == "/lessons":
        rows = lessons(user.id)
        return "\n".join(f"{i + 1}. {_lesson_text(r)}" for i, r in enumerate(rows)) or \
            "No lessons yet. Tell me things like “always make videos portrait”."
    if cmd == "/forget":
        rows = lessons(user.id)
        try:
            row = rows[int(rest) - 1]
        except (ValueError, IndexError):
            return "Use /forget <number> from /lessons."
        db.run("DELETE FROM agent_lessons WHERE id=?", (row["id"],))
        return f"Forgot: {row['text']}"
    if cmd == "/remember":
        if not rest.strip():
            return "Use /remember <rule>."
        _add_lesson(user.id, rest, thread["project_id"])
        return (f"I'll remember for this story: {rest.strip()}" if thread["project_id"]
                else f"I'll remember: {rest.strip()}")
    if cmd == "/help":
        return ("/status — what's running · /review — planner checks now · /stop — stop the "
                "assistant · /remember <rule> · /lessons · /forget <n>")
    return None


# =========================================================================== views + API

def _sign(kind: str, item_id: str, base: str) -> str:
    return base + service.stable_signed_path(kind, item_id, 24 * 3600)


def _media_view(items: list, base: str) -> list:
    out = []
    for m in items or []:
        url = m.get("url")
        if m.get("kind") and m.get("id"):
            url = _sign(m["kind"], m["id"], base)
        if url:
            out.append({"type": m["type"], "url": url, "kind": m.get("kind"), "id": m.get("id")})
    return out


def _message_view(r, base: str) -> dict:
    meta = json.loads(r["meta"] or "{}")
    if "media" in meta:
        meta["media"] = _media_view(meta["media"], base)
    if "result" in meta:
        meta["result"] = _digest(meta["result"], 1500)
    if meta.get("files"):
        rows = [db.one("SELECT * FROM agent_files WHERE id=?", (f,)) for f in meta["files"]]
        meta["files"] = [_file_view(f, base) for f in rows if f is not None]
    return {"seq": r["seq"], "role": r["role"], "content": r["content"], "meta": meta,
            "at": r["created_at"], "updated": r["updated_at"]}


def _file_view(row, base: str) -> dict:
    out = agentfiles.view(row)
    i = agentfiles.info(row)
    out["url"] = f"/api/agent/files/{row['id']}"
    if i.get("image_id"):
        out["thumb"] = _sign("image", i["image_id"], base)
    elif i.get("frames"):
        out["thumb"] = f"/api/agent/files/{row['id']}?frame=1"
    if row["text"]:
        out["excerpt"] = row["text"][:280]
    return out


def _live(t: dict) -> dict:
    """A tracked job's state read straight from the studio tables, for the page."""
    t = dict(t)
    if t["kind"] == "clip":
        r = _clip_status(t["id"])
        t["status"] = r["status"]
        return t
    table = {"project": "projects", "image": "images", "speech": "speech", "music": "music",
             "voice_job": "voice_jobs"}.get(t["kind"])
    if table:
        cols = "status, progress, message" if table in ("projects", "images") else "status, message"
        try:
            r = db.one(f"SELECT {cols} FROM {table} WHERE id=?", (t["id"],))
        except Exception:                                      # noqa: BLE001
            r = None
        if r is not None:
            t["status"] = r["status"]
            if "progress" in r.keys():
                t["progress"] = r["progress"]
            if r["message"]:
                t["message"] = r["message"]
        if table in ("speech", "music") and t["status"] == "queued":
            pos = gpuqueue.position(table, t["id"])
            if pos:
                t["message"] = f"in the queue, #{pos}"
    return t


def _assets(thread_id: str, base: str) -> list[dict]:
    """Everything this conversation made, newest first, one entry per item."""
    seen, out = set(), []
    for r in db.query("SELECT seq, meta FROM agent_messages WHERE thread_id=? AND role IN "
                      "('step','result') ORDER BY seq DESC", (thread_id,)):
        meta = json.loads(r["meta"] or "{}")
        for m in meta.get("media") or []:
            key = (m.get("kind"), m.get("id"), m.get("url"))
            if key in seen:
                continue
            seen.add(key)
            v = _media_view([m], base)
            if v:
                label = {"image": "picture", "video": "video", "speech": "speech",
                         "music": "song"}.get(m.get("kind") or "", m["type"])
                out.append({**v[0], "label": label, "step": meta.get("n"), "seq": r["seq"]})
    return out


def _context_view(row, base: str) -> list[dict]:
    out = []
    for c in _context_items(row):
        item = _library_item(row["user_id"], c.get("kind"), c.get("id"))
        if item is None:
            continue
        v = {"kind": item["kind"], "id": item["id"], "title": item["title"]}
        if item["kind"] == "image":
            v["thumb"] = _sign("image", item["id"], base)
        out.append(v)
    return out


def thread_view(row, base: str, since: float = 0.0) -> dict:
    msgs = db.query("SELECT * FROM agent_messages WHERE thread_id=? AND updated_at > ? "
                    "ORDER BY seq", (row["id"], since))
    return {"id": row["id"], "title": row["title"], "status": row["status"],
            "activity": row["activity"], "thought": row["thought"] or "",
            "stage_at": row["stage_at"], "tracked": [_live(t) for t in _tracked(row)],
            "next_review_at": row["next_review_at"], **service.retention(row["updated_at"]),
            "files": [_file_view(f, base) for f in agentfiles.files_of(row["id"])],
            "assets": _assets(row["id"], base), "context": _context_view(row, base),
            "messages": [_message_view(m, base) for m in msgs], "now": _now()}


def threads_of(user: auth.User) -> list:
    return db.query("SELECT id, title, status, updated_at FROM agent_threads WHERE user_id=? "
                    "ORDER BY updated_at DESC LIMIT 60", (user.id,))


def delete_thread(thread_id: str) -> None:
    stop(thread_id)
    agentfiles.delete_for_thread(thread_id)
    db.run("DELETE FROM agent_threads WHERE id=?", (thread_id,))


def _user(request: Request) -> auth.User:
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(status_code=401)
    return user


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


NEW_TITLE = "New conversation"


@router.get("/api/agent/threads")
async def api_threads(request: Request):
    user = _user(request)
    return {"threads": [{"id": r["id"], "title": r["title"], "status": r["status"],
                         "updated_at": r["updated_at"]} for r in threads_of(user)],
            "lessons": [{"id": r["id"], "text": _lesson_text(r)} for r in lessons(user.id)],
            "planner": cfg.PLANNER_MODEL, "executor": cfg.EXECUTOR_MODEL, "now": _now()}


@router.get("/api/agent/threads/{thread_id}")
async def api_thread(thread_id: str, request: Request, since: float = 0.0):
    user = _user(request)
    return thread_view(owned_thread(user, thread_id), service.base_url(request), since)


def _new_thread(user, request: Request, title: str = NEW_TITLE) -> str:
    tid = db.new_id("thr")
    now = _now()
    db.run("INSERT INTO agent_threads (id, user_id, title, status, base_url, created_at, "
           "updated_at) VALUES (?,?,?,?,?,?,?)",
           (tid, user.id, title, "idle", service.base_url(request), now, now))
    return tid


@router.post("/api/agent/threads")
async def api_new_thread(request: Request):
    """{text, files?} starts a conversation; {} makes an empty one to attach files to."""
    user = _user(request)
    body = await _body(request)
    text = str(body.get("text") or "").strip()
    tid = _new_thread(user, request)
    if not text and not body.get("files"):
        return {"id": tid}
    return await _say(user, _thread(tid), text, body.get("files") or [])


@router.post("/api/agent/threads/{thread_id}/messages")
async def api_message(thread_id: str, request: Request):
    user = _user(request)
    row = owned_thread(user, thread_id)
    body = await _body(request)
    text = str(body.get("text") or "").strip()
    files = body.get("files") or []
    if not text and not files:
        raise HTTPException(400, "empty message")
    if row["base_url"] != service.base_url(request):
        _set(thread_id, base_url=service.base_url(request))
    focus = body.get("focus") if isinstance(body.get("focus"), dict) else None
    if focus:
        focus = {"level": str(focus.get("level") or ("shot" if focus.get("shot_id") else "story"))[:8],
                 "shot_id": str(focus.get("shot_id") or "")[:40],
                 "scene_id": str(focus.get("scene_id") or "")[:40],
                 "label": str(focus.get("label") or "")[:80]}
    return await _say(user, _thread(thread_id), text, files, focus)


async def _say(user, row, text: str, file_ids: list, focus: dict | None = None) -> dict:
    text = text[:60000]          # a whole script can be pasted (was 8,000; 2026-11-25)
    last = db.one("SELECT content, created_at FROM agent_messages WHERE thread_id=? AND role='user' "
                  "ORDER BY seq DESC LIMIT 1", (row["id"],))
    if last and not file_ids and last["content"] == text and _now() - last["created_at"] < 5:
        return {"id": row["id"], "duplicate": True}     # the same message twice in a row
    ids = []
    for f in file_ids if isinstance(file_ids, list) else []:
        r = db.one("SELECT id FROM agent_files WHERE id=? AND thread_id=?", (str(f), row["id"]))
        if r is not None:
            ids.append(r["id"])
    if row["title"] == NEW_TITLE:
        names = [db.one("SELECT name FROM agent_files WHERE id=?", (i,))["name"] for i in ids]
        _set(row["id"], title=(" ".join(text.split()) or ", ".join(names) or NEW_TITLE)[:70])
    _post(row["id"], "user", text, **({"files": ids} if ids else {}),
          **({"focus": focus} if focus and (focus.get("shot_id") or focus.get("scene_id")
                                            or focus.get("level") == "story") else {}))
    if text.startswith("/") and not ids:
        answer = _command(user, row, text)
        if answer is not None:
            _post(row["id"], "note", answer, command=True)
            return {"id": row["id"]}
    if row["status"] in ("planning", "executing"):
        # Mid-turn: the planner reads this on its next call, and the turn re-plans
        # after it ends if it never did.
        _post(row["id"], "note", "Got it — I'll take that into account at the next step.")
        _set(row["id"], reason="the person sent a new message (see above); follow it")
        return {"id": row["id"]}
    _set(row["id"], corrections=0)
    _queue_turn(row["id"], "new message from the person")
    return {"id": row["id"]}


@router.post("/api/agent/threads/{thread_id}/files")
async def api_upload(thread_id: str, request: Request):
    user = _user(request)
    owned_thread(user, thread_id)
    form = await request.form()
    uploads = [v for k, v in form.multi_items() if k == "files" and hasattr(v, "read")]
    if not uploads:
        raise HTTPException(400, "no files")
    base = service.base_url(request)
    out = []
    for up in uploads:
        view = await agentfiles.save(user.id, thread_id, up)
        out.append(_file_view(db.one("SELECT * FROM agent_files WHERE id=?", (view["id"],)), base))
    _set(thread_id)
    return {"files": out}


@router.get("/api/agent/files/{file_id}")
async def api_file(file_id: str, request: Request, frame: int | None = None):
    from fastapi.responses import FileResponse
    user = _user(request)
    row = db.one("SELECT * FROM agent_files WHERE id=? AND user_id=?", (file_id, user.id))
    if row is None:
        raise HTTPException(404, "no such file")
    if frame is not None:
        frames = agentfiles.info(row).get("frames") or []
        if not 0 <= frame < len(frames) or not Path(frames[frame]).exists():
            raise HTTPException(404, "no such frame")
        return FileResponse(frames[frame], media_type="image/jpeg")
    if not Path(row["path"]).exists():
        raise HTTPException(404, "the file is gone")
    return FileResponse(row["path"], media_type=row["mime"] or None, filename=row["name"],
                        content_disposition_type="inline")


@router.post("/api/agent/files/{file_id}/delete")
async def api_file_delete(file_id: str, request: Request):
    user = _user(request)
    row = db.one("SELECT * FROM agent_files WHERE id=? AND user_id=?", (file_id, user.id))
    if row is None:
        raise HTTPException(404, "no such file")
    agentfiles.delete_paths([row])
    db.run("DELETE FROM agent_files WHERE id=?", (file_id,))
    return {"ok": True}


@router.get("/api/agent/library")
async def api_library(request: Request):
    """The person's recent studio items, to pin into a conversation's context."""
    user = _user(request)
    base = service.base_url(request)
    out = {"images": [], "videos": [], "speech": [], "music": []}
    for r in db.query("SELECT id, prompt, width, height FROM images WHERE user_id=? AND "
                      "status='succeeded' ORDER BY created_at DESC LIMIT 36", (user.id,)):
        out["images"].append({"kind": "image", "id": r["id"], "title": (r["prompt"] or "")[:120],
                              "thumb": _sign("image", r["id"], base)})
    for r in db.query("SELECT id, title, mode, status FROM projects WHERE user_id=? "
                      "ORDER BY updated_at DESC LIMIT 20", (user.id,)):
        asset = db.one("SELECT id FROM assets WHERE project_id=? AND kind='image' ORDER BY idx "
                       "LIMIT 1", (r["id"],))
        out["videos"].append({"kind": "project", "id": r["id"], "title": r["title"],
                              "status": r["status"], "mode": r["mode"],
                              **({"thumb": f"/projects/{r['id']}/asset/{asset['id']}"}
                                 if asset else {}),
                              **({"url": _sign("video", r["id"], base)}
                                 if r["status"] == "succeeded" else {})})
    for r in db.query("SELECT id, text, voice FROM speech WHERE user_id=? AND status='succeeded' "
                      "ORDER BY created_at DESC LIMIT 20", (user.id,)):
        out["speech"].append({"kind": "speech", "id": r["id"], "title": (r["text"] or "")[:120],
                              "sub": r["voice"], "url": _sign("speech", r["id"], base)})
    for r in db.query("SELECT id, title, task, duration_s FROM music WHERE user_id=? AND "
                      "status='succeeded' ORDER BY created_at DESC LIMIT 20", (user.id,)):
        out["music"].append({"kind": "music", "id": r["id"], "title": r["title"] or "song",
                             "sub": f"{r['task']} · {round(r['duration_s'] or 0)} s",
                             "url": _sign("music", r["id"], base)})
    return out


@router.post("/api/agent/threads/{thread_id}/context")
async def api_context(thread_id: str, request: Request):
    """{kind, id, remove?}: pin or unpin a library item in this conversation's context."""
    user = _user(request)
    row = owned_thread(user, thread_id)
    body = await _body(request)
    kind, item_id = str(body.get("kind") or ""), str(body.get("id") or "")
    items = [c for c in _context_items(row) if not (c["kind"] == kind and c["id"] == item_id)]
    if not body.get("remove"):
        if _library_item(user.id, kind, item_id) is None:
            raise HTTPException(404, "that item isn't in your library (or isn't finished)")
        if len(items) >= 12:
            raise HTTPException(400, "at most 12 pinned items; unpin one first")
        items.append({"kind": kind, "id": item_id})
    _set(thread_id, context=json.dumps(items))
    return {"context": _context_view(_thread(thread_id), service.base_url(request))}


@router.post("/api/agent/threads/{thread_id}/stop")
async def api_stop(thread_id: str, request: Request):
    user = _user(request)
    owned_thread(user, thread_id)
    stop(thread_id)
    _post(thread_id, "note", "Stopped by you.")
    return {"ok": True}


@router.post("/api/agent/threads/{thread_id}/delete")
async def api_delete(thread_id: str, request: Request):
    user = _user(request)
    owned_thread(user, thread_id)
    delete_thread(thread_id)
    return {"ok": True}


@router.post("/api/agent/threads/{thread_id}/rename")
async def api_rename(thread_id: str, request: Request):
    user = _user(request)
    owned_thread(user, thread_id)
    title = " ".join(str((await _body(request)).get("title") or "").split())[:70]
    if title:
        _set(thread_id, title=title)
    return {"ok": True}


@router.post("/api/agent/lessons/{lesson_id}/delete")
async def api_forget(lesson_id: str, request: Request):
    user = _user(request)
    db.run("DELETE FROM agent_lessons WHERE id=? AND user_id=?", (lesson_id, user.id))
    return {"ok": True}


@router.post("/api/agent/lessons")
async def api_remember(request: Request):
    user = _user(request)
    _add_lesson(user.id, str((await _body(request)).get("text") or ""))
    return {"ok": True}
