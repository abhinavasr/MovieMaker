"""SQLite schema and access.

One writer (the render loop) and a handful of readers (the request handlers),
so WAL plus a short busy timeout is enough; there is no connection pool and no
ORM on purpose. Every function opens, does its work and closes, which keeps a
long-running render from holding a transaction across an await.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from app.config import cfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    created_at    REAL NOT NULL,
    expires_at    REAL,
    disabled      INTEGER NOT NULL DEFAULT 0,
    -- 1 = exempt from BOTH hourly limits (renders and pictures). Not from the
    -- studio's WIS key bucket, which no setting here can lift.
    unlimited     INTEGER NOT NULL DEFAULT 0,
    note          TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    title       TEXT NOT NULL,
    mode        TEXT NOT NULL,
    story       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'draft',
    progress    INTEGER NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT '',
    error       TEXT,
    params_json TEXT NOT NULL DEFAULT '{}',
    output_path TEXT,
    has_audio   INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS projects_user ON projects(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS assets (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    path       TEXT NOT NULL,
    orig_name  TEXT NOT NULL DEFAULT '',
    mime       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS assets_project ON assets(project_id, kind, idx);
CREATE TABLE IF NOT EXISTS shots (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    -- `title` is the person's working label for the shot; `notes` is whatever
    -- WIS said about the job. They were one column at first and the service's
    -- notes overwrote the label on every submit.
    title      TEXT NOT NULL DEFAULT '',
    prompt     TEXT NOT NULL DEFAULT '',
    seconds    INTEGER NOT NULL DEFAULT 5,
    asset_id   TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',
    wis_job_id TEXT,
    clip_path  TEXT,
    error      TEXT,
    notes      TEXT
);
CREATE INDEX IF NOT EXISTS shots_project ON shots(project_id, idx);
CREATE TABLE IF NOT EXISTS renders (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    project_id TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS renders_user ON renders(user_id, created_at);
-- Pictures made on the GPU. They belong to a person, not a project: a picture
-- is often made before there is a project to put it in, and the same one can
-- go into several. `add_file(image_id=...)` copies it into a project.
CREATE TABLE IF NOT EXISTS images (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    sources     INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'queued',
    progress    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    notes       TEXT,
    wis_job_id  TEXT,
    path        TEXT,
    width       INTEGER,
    height      INTEGER,
    seed        INTEGER,
    references_applied INTEGER,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS images_user ON images(user_id, created_at DESC);
-- Speech from the studio's Qwen3-TTS container. Like pictures, it belongs to a
-- person; `add_file(speech_id=...)` copies a clip into a project as audio.
CREATE TABLE IF NOT EXISTS speech (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    text        TEXT NOT NULL,
    voice       TEXT NOT NULL,
    language    TEXT NOT NULL DEFAULT 'Auto',
    instruct    TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',
    error       TEXT,
    path        TEXT,
    duration_s  REAL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS speech_user ON speech(user_id, created_at DESC);
-- Songs from the studio's ACE-Step container. `params` is what was asked
-- (JSON); `result_meta` is what came back (final lyrics, bpm, key...).
CREATE TABLE IF NOT EXISTS music (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    task        TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    params      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    message     TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    path        TEXT,
    duration_s  REAL,
    result_meta TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS music_user ON music(user_id, created_at DESC);
-- Personal voices (app/voices.py). A voice row and its profile are kept until the
-- owner deletes it; clips and jobs expire with everything else.
CREATE TABLE IF NOT EXISTS voices (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'collecting',
    consent_version TEXT NOT NULL,
    consent_at      REAL NOT NULL,
    consent_ip      TEXT,
    epochs_done     INTEGER NOT NULL DEFAULT 0,
    epochs_target   INTEGER NOT NULL DEFAULT 0,
    slices_done     INTEGER NOT NULL DEFAULT 0,
    profile         TEXT,
    last_error      TEXT,
    trained_at      REAL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS voices_user ON voices(user_id);
CREATE TABLE IF NOT EXISTS voice_clips (
    id              TEXT PRIMARY KEY,
    voice_id        TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'auto',
    status          TEXT NOT NULL DEFAULT 'cleaning',
    analysis        TEXT,
    rejected_reason TEXT,
    error           TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS voice_clips_voice ON voice_clips(voice_id);
CREATE TABLE IF NOT EXISTS voice_jobs (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    voice_id     TEXT NOT NULL,
    kind         TEXT NOT NULL,
    params       TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'queued',
    stage        TEXT,
    progress     REAL,
    message      TEXT,
    error        TEXT,
    result       TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    wait_limit_s REAL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS voice_jobs_user ON voice_jobs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS voice_jobs_status ON voice_jobs(status, created_at);
-- Singing-style models (app/styles.py). The row + its adapter are kept with the voice;
-- the uploaded songs expire.
CREATE TABLE IF NOT EXISTS style_models (
    id            TEXT PRIMARY KEY,
    voice_id      TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'collecting',
    epochs_target INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    trained_at    REAL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS style_models_voice ON style_models(voice_id);
CREATE TABLE IF NOT EXISTS style_songs (
    id             TEXT PRIMARY KEY,
    style_model_id TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    title          TEXT NOT NULL,
    duration_s     REAL,
    lyrics         TEXT,
    lyrics_source  TEXT,
    status         TEXT NOT NULL DEFAULT 'transcribing',
    error          TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS style_songs_model ON style_songs(style_model_id);
-- OAuth for the MCP endpoint. Clients register themselves (ChatGPT, Claude and
-- the rest all do dynamic registration), so a client row proves nothing about
-- who is behind it -- the grant is always to a studio USER, who signs in.
-- Codes and tokens are stored as SHA-256 only, like the session passwords.
CREATE TABLE IF NOT EXISTS oauth_clients (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    redirect_uris TEXT NOT NULL,
    secret_hash   TEXT,
    created_at    REAL NOT NULL,
    last_used_at  REAL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash    TEXT PRIMARY KEY,
    client_id    TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    challenge    TEXT NOT NULL,
    expires_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    client_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_tokens_user ON oauth_tokens(user_id);
-- Device sign-in (RFC 8628): a short user_code approved at /activate.
CREATE TABLE IF NOT EXISTS oauth_device (
    device_hash TEXT PRIMARY KEY,
    user_code   TEXT NOT NULL UNIQUE,
    client_id   TEXT NOT NULL,
    user_id     TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    interval    INTEGER NOT NULL DEFAULT 5,
    last_poll   REAL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_threads (
    id             TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title          TEXT NOT NULL DEFAULT '',
    -- idle | queued (wants the planner) | planning | executing | watching | stopped
    status         TEXT NOT NULL DEFAULT 'idle',
    activity       TEXT NOT NULL DEFAULT '',
    reason         TEXT NOT NULL DEFAULT '',
    tracked        TEXT NOT NULL DEFAULT '[]',
    corrections    INTEGER NOT NULL DEFAULT 0,
    next_review_at REAL,
    base_url       TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_messages (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id  TEXT NOT NULL REFERENCES agent_threads(id) ON DELETE CASCADE,
    -- user | assistant | plan | step | note | error
    role       TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    meta       TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_messages_thread ON agent_messages(thread_id, seq);
CREATE TABLE IF NOT EXISTS agent_files (
    id         TEXT PRIMARY KEY,
    thread_id  TEXT NOT NULL REFERENCES agent_threads(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL,
    num        INTEGER NOT NULL,          -- F1, F2 ... within the conversation
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,             -- image | audio | video | pdf | docx | text
    mime       TEXT NOT NULL DEFAULT '',
    size       INTEGER NOT NULL,
    path       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'processing',   -- processing | ready | failed
    info       TEXT NOT NULL DEFAULT '{}',
    text       TEXT,
    error      TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_files_thread ON agent_files(thread_id, num);
-- The production editor (app/editor.py). A production is a storyboard project;
-- its shots are grouped into scenes, it has references (characters, places,
-- voices) and a timeline of audio clips that is mixed over the joined shots.
CREATE TABLE IF NOT EXISTS prod_scenes (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    summary    TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS prod_refs (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    kind        TEXT NOT NULL,            -- character | location | prop | style | voice
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    image_id    TEXT,                     -- a studio picture
    voice_path  TEXT,                     -- a voice sample to clone
    speaker     TEXT,                     -- or a preset speaker
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS prod_clips (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    track        TEXT NOT NULL,           -- dialogue | ambience | music
    name         TEXT NOT NULL DEFAULT '',
    source_kind  TEXT NOT NULL,           -- speech | music | upload
    source_id    TEXT,
    path         TEXT,
    status       TEXT NOT NULL DEFAULT 'ready',   -- pending | ready | failed
    error        TEXT,
    start_s      REAL NOT NULL DEFAULT 0,
    trim_s       REAL NOT NULL DEFAULT 0,
    duration_s   REAL,                    -- length used on the timeline
    source_s     REAL,                    -- the file's own length
    gain_db      REAL NOT NULL DEFAULT 0,
    fade_in      REAL NOT NULL DEFAULT 0,
    fade_out     REAL NOT NULL DEFAULT 0,
    use_in_video INTEGER NOT NULL DEFAULT 0,
    ref_id       TEXT,                    -- the character speaking
    text         TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    shot_id    TEXT,
    title      TEXT NOT NULL,
    summary    TEXT NOT NULL DEFAULT '',
    changes    TEXT NOT NULL DEFAULT '[]',
    options    TEXT NOT NULL DEFAULT '[]',
    status     TEXT NOT NULL DEFAULT 'open',      -- open | applied | dismissed
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_lessons (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Batiyao (batiyao.com) accounts linked to studio users: sign-in and sharing. Tokens are
-- encrypted at rest (app/batiyao.py) and never sent to the browser.
CREATE TABLE IF NOT EXISTS batiyao_links (
    user_id       TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    bt_user_id    TEXT NOT NULL UNIQUE,
    username      TEXT NOT NULL DEFAULT '',
    display_name  TEXT NOT NULL DEFAULT '',
    access_token  TEXT NOT NULL DEFAULT '',
    refresh_token TEXT NOT NULL DEFAULT '',
    scopes        TEXT NOT NULL DEFAULT '',
    expires_at    REAL NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
-- One row per sign-in in flight (PKCE verifier + state), kept 15 minutes.
CREATE TABLE IF NOT EXISTS batiyao_pending (
    state      TEXT PRIMARY KEY,
    verifier   TEXT NOT NULL,
    purpose    TEXT NOT NULL,              -- login | connect
    user_id    TEXT,
    next       TEXT NOT NULL DEFAULT '/',
    created_at REAL NOT NULL
);
-- Podcast runs (app/podcast.py): research -> fact-check -> debate points -> script ->
-- an editor production. `state` holds every agent's output, so a restart resumes mid-run.
CREATE TABLE IF NOT EXISTS podcasts (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT NOT NULL DEFAULT '',
    topic       TEXT NOT NULL,
    config      TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL,              -- queued|researching|review_research|debating|
                                            -- review_points|writing|building|done|failed|cancelled
    message     TEXT NOT NULL DEFAULT '',
    progress    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    state       TEXT NOT NULL DEFAULT '{}',
    log         TEXT NOT NULL DEFAULT '[]',
    project_id  TEXT,
    thread_id   TEXT,                       -- the assistant conversation that asked for it
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS podcasts_user ON podcasts(user_id, created_at);

-- What was shared, for the person's own record and support.
CREATE TABLE IF NOT EXISTS batiyao_shares (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    mode       TEXT NOT NULL,              -- post | story
    status     TEXT NOT NULL,
    result     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(cfg.db_path, timeout=20.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn) -> None:
    """Add columns that a database created by an older build is missing.

    `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so
    a schema change alone would leave an existing file one column short and
    every insert would fail at runtime rather than here.
    """
    wanted = {
              "users": {"unlimited": "INTEGER NOT NULL DEFAULT 0"},
              "images": {"model": "TEXT"},
              "agent_threads": {"thought": "TEXT NOT NULL DEFAULT ''", "stage_at": "REAL",
                                "context": "TEXT NOT NULL DEFAULT '[]'", "project_id": "TEXT"},
              "prod_refs": {"angles": "TEXT NOT NULL DEFAULT '{}'",
                            # designed voices (2026-11-25): the voice description, what the
                            # sample says (for cloning), the design job, design automatically
                            "voice_design": "TEXT NOT NULL DEFAULT ''",
                            "voice_text": "TEXT", "voice_job": "TEXT",
                            "voice_auto": "INTEGER NOT NULL DEFAULT 0",
                            # props/styles that go with a character or place (2026-11-25)
                            "links": "TEXT NOT NULL DEFAULT '[]'",
                            # the person has chosen this character's look (2026-11-25); existing
                            # rows count as chosen, the assistant's new characters start at 0
                            "approved": "INTEGER NOT NULL DEFAULT 1"},
              "agent_lessons": {"project_id": "TEXT"},
              "prod_clips": {"instruct": "TEXT NOT NULL DEFAULT ''", "auto": "INTEGER NOT NULL DEFAULT 0"},
              "prod_scenes": {"refs": "TEXT NOT NULL DEFAULT '[]'",
                              # per character, the props they have in this scene (2026-11-25)
                              "wear": "TEXT NOT NULL DEFAULT '{}'",
                              "locked": "INTEGER NOT NULL DEFAULT 0"},
              "shots": {"title": "TEXT NOT NULL DEFAULT ''", "scene_id": "TEXT",
                        "framing": "TEXT NOT NULL DEFAULT ''", "refs": "TEXT NOT NULL DEFAULT '[]'",
                        "ref_videos": "TEXT NOT NULL DEFAULT '[]'",
                        "opts": "TEXT NOT NULL DEFAULT '{}'",
                        "make": "TEXT NOT NULL DEFAULT 'animate'",
                        "start_from": "TEXT NOT NULL DEFAULT ''",
                        "cont_checked": "INTEGER NOT NULL DEFAULT 0"},
              "speech": {"reference_text": "TEXT", "message": "TEXT",
                         "attempts": "INTEGER NOT NULL DEFAULT 0", "params": "TEXT"}}
    for table, columns in wanted.items():
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def query(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with connect() as conn:
        return list(conn.execute(sql, args))


def one(sql: str, args: tuple = ()) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(sql, args).fetchone()


def run(sql: str, args: tuple = ()) -> None:
    with connect() as conn:
        conn.execute(sql, args)


_secret: str | None = None


def secret() -> str:
    """The key that signs download and upload links. Made once, kept in the db.

    In the database rather than `.env` so that an existing install gets one
    without the operator editing anything, and so it survives a rebuild.
    """
    global _secret
    if _secret is None:
        run("INSERT OR IGNORE INTO meta (key, value) VALUES ('link_secret', ?)",
            (uuid.uuid4().hex + uuid.uuid4().hex,))
        _secret = one("SELECT value FROM meta WHERE key='link_secret'")["value"]
    return _secret


def params_of(row: Any) -> dict:
    try:
        return json.loads(row["params_json"] or "{}")
    except (TypeError, ValueError, KeyError):
        return {}


def set_params(project_id: str, params: dict) -> None:
    run("UPDATE projects SET params_json=?, updated_at=? WHERE id=?",
        (json.dumps(params), now(), project_id))


def touch_project(project_id: str, **fields: Any) -> None:
    """Update a project row. Deliberately narrow: only these columns move."""
    allowed = {"status", "progress", "message", "error", "output_path", "title",
               "story", "has_audio"}
    sets, args = [], []
    for key, value in fields.items():
        if key not in allowed:
            raise KeyError(f"touch_project does not write '{key}'")
        sets.append(f"{key}=?")
        args.append(value)
    if not sets:
        return
    sets.append("updated_at=?")
    args.append(now())
    args.append(project_id)
    run(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", tuple(args))
