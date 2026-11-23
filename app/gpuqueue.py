"""One queue for the GPU work the studio runs itself: speech and music.

Pictures and videos go through WIS, which has its own queue. Speech (the `tts`
container) and music (the `music` container) do not, and they share the same
24 GB card with WIS. So instead of refusing when the card is full, a request is
saved as `queued` and this single worker runs the jobs one at a time, oldest
first, each only when:

- WIS has nothing running or queued (its /v1/health says so), and
- no studio video render is in progress -- a storyboard submits its shots one at
  a time, so WIS looks idle for a moment between shots, and
- WIS reports enough free VRAM for that kind of job.

WIS fails its own job with "busy" if it runs out of memory, so starting on top
of it would break someone else's picture. An engine that still runs out
(another tenant arrived meanwhile) answers 503; the job goes back in the queue
rather than failing. The queue lives in the database: a restart puts anything
that was mid-flight back to `queued` and carries on.
"""
from __future__ import annotations

import asyncio
import logging

import time

from app import db, wis
from app.config import cfg

log = logging.getLogger("studio.gpuqueue")

# Free VRAM (MB) a job needs before it may start, measured with headroom.
# voice_jobs: RVC training peaked at 6.5 GB; a new song needs the music engine first.
NEED_MB = {"speech": 7000, "music": 14000, "voice_jobs": 14000}   # measured peaks: ~5, ~10.5 GB
STYLE_TRAIN_MB = 21000        # measured peak ~18.5 GB for XL LoKr training
BURMESE_SPEECH_MB = 9500      # VoxCPM2 (tts-my): measured 8.2 GB reserved at peak
MAX_WAIT_S = 45 * 60          # a job that never gets the card fails after this
MAX_ATTEMPTS = 4              # out-of-memory retries before giving up
# Unlimited accounts (the owner) get no artificial limits here (2026-11-21): no wait
# timeout, no retry cap, and no "wait until WIS is idle" rule. Their job takes a turn
# in WIS's own queue instead (a GPU lease): WIS finishes the picture or video in
# hand, unloads its models, and lends the card. Before this, 79 of the owner's
# speech clips waited 40 minutes behind a steady stream of their own picture jobs,
# because WIS was never idle and kept its image model resident between jobs.
# Renewed before every job, so these only bound how long a lease outlives a crash.
LEASE_HOLD_S = {"speech": 300, "music": 1200, "voice_jobs": 4 * 3600}
# Back-to-back owner jobs share one lease rather than making WIS unload and reload
# its image model per clip; after this long the card goes back so the owner's own
# pictures and videos keep moving too (they interleave, nobody starves).
LEASE_REUSE_S = 300
_held: dict = {"id": None, "since": 0.0}


def _lease_file():
    return cfg.DATA_DIR / "gpu_lease.txt"


def _remember(lease_id: str | None) -> None:
    """On disk, so a restart can hand back a lease this process was holding."""
    f = _lease_file()
    if f is None:
        return
    try:
        if lease_id:
            f.write_text(lease_id)
        else:
            f.unlink(missing_ok=True)
    except OSError:
        pass


async def release_leftover_lease() -> None:
    f = _lease_file()
    if f is not None and f.exists():
        lease_id = f.read_text().strip()
        if lease_id:
            log.info("returning GPU lease %s left by the previous process", lease_id)
            await wis.release_gpu(lease_id)
        _remember(None)


async def _drop_lease() -> None:
    if _held["id"]:
        await wis.release_gpu(_held["id"])
    _held["id"], _held["since"] = None, 0.0
    _remember(None)
POLL_S = 5

# The assistant's planner (app/agent.py) needs most of the card for about a
# minute. While it is paused, no new speech/music/voice job starts here and any
# idle owner lease is handed back; a job already generating finishes first
# (`idle()` tells the planner when it has).
_pause = {"n": 0}
_running = {"n": 0}


def pause() -> None:
    _pause["n"] += 1


def resume() -> None:
    _pause["n"] = max(0, _pause["n"] - 1)
    kick()


def paused() -> bool:
    return _pause["n"] > 0


def idle() -> bool:
    """True when no studio speech/music/voice job is on the card right now."""
    return _running["n"] == 0

TABLES = ("speech", "music", "voice_jobs")
_wake = asyncio.Event()
_handlers: dict = {}


class Busy(Exception):
    """The engine ran out of GPU memory; put the job back and try later."""


def register(kind: str, handler) -> None:
    """`handler(row)` does the work; it raises Busy, or any error to fail the job."""
    _handlers[kind] = handler


def kick() -> None:
    _wake.set()


def position(kind: str, row_id: str) -> int | None:
    """1-based place in the shared queue, or None when not waiting."""
    row = db.one(f"SELECT created_at, status FROM {kind} WHERE id=?", (row_id,))
    if row is None or row["status"] != "queued":
        return None
    ahead = sum(db.one(f"SELECT COUNT(*) AS n FROM {t} WHERE status IN ('queued','generating') "
                       "AND created_at < ?", (row["created_at"],))["n"] for t in TABLES)
    return ahead + 1


def resume_interrupted() -> None:
    for t in TABLES:
        db.run(f"UPDATE {t} SET status='queued', updated_at=? WHERE status='generating'",
               (db.now(),))


def _next():
    # voice_jobs rows carry their own wait limit (a training run may wait for days).
    # Style training (~20 GB for up to ~90 min) is invisible outside its night window,
    # so it never blocks the head of the queue while it waits.
    lo, hi = cfg.STYLE_WINDOW
    hour = time.gmtime().tm_hour
    in_window = lo <= hour < hi if lo < hi else (hour >= lo or hour < hi)
    rows = []
    for t in TABLES:
        extra = "wait_limit_s, kind AS job_kind" if t == "voice_jobs" else \
            "NULL AS wait_limit_s, NULL AS job_kind"
        skip = "" if t != "voice_jobs" or in_window else " AND kind != 'style_train'"
        rows.append(db.one(f"SELECT '{t}' AS kind, id, created_at, attempts, {extra} FROM {t} "
                           f"WHERE status='queued'{skip} ORDER BY created_at LIMIT 1"))
    rows = [r for r in rows if r is not None]
    return min(rows, key=lambda r: r["created_at"]) if rows else None


def _owner_unlimited(kind: str, row_id: str) -> bool:
    from app import auth
    row = db.one(f"SELECT user_id FROM {kind} WHERE id=?", (row_id,))
    return bool(row and auth.is_unlimited(row["user_id"]))


def _studio_rendering() -> bool:
    row = db.one("SELECT COUNT(*) AS n FROM projects WHERE status IN ('running','queued')")
    return bool(row and row["n"])


def _need_mb(kind: str, row_id: str, job_kind) -> int | None:
    """A per-job VRAM need where it differs from the table's NEED_MB."""
    if job_kind == "style_train":
        return STYLE_TRAIN_MB
    if kind == "speech":
        row = db.one("SELECT language, voice FROM speech WHERE id=?", (row_id,))
        from app import speech
        if row and speech.uses_voxcpm(row["language"], row["voice"]):
            return BURMESE_SPEECH_MB
        if row and row["language"] == speech.SANSKRIT:
            return 3500          # Vāgdhenu: measured 2.4 GB
    return None


async def _gpu_ready(kind: str, need_mb: int | None = None) -> tuple[bool, str]:
    if _studio_rendering():
        return False, "waiting for a video render to finish"
    try:
        health = await wis.health()
    except Exception:                                          # noqa: BLE001
        # WIS down means nothing of its is on the card; the engine's own
        # out-of-memory answer still protects the job.
        return True, ""
    queue = health.get("queue") or {}
    if (queue.get("running") or 0) + (queue.get("queued") or 0) > 0:
        return False, "waiting for pictures/videos ahead of it to finish"
    free = (health.get("gpu") or {}).get("vram_free_mb")
    if free is not None and free < (need_mb or NEED_MB[kind]):
        return False, "waiting for the GPU to free up"
    return True, ""


def _set(kind: str, row_id: str, **fields) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    db.run(f"UPDATE {kind} SET {sets}, updated_at=? WHERE id=?",
           (*fields.values(), db.now(), row_id))


async def worker() -> None:
    resume_interrupted()
    await release_leftover_lease()
    while True:
        try:
            job = _next()
            if job is None:
                await _drop_lease()
                _wake.clear()
                try:
                    await asyncio.wait_for(_wake.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                continue
            if paused():
                await _drop_lease()        # the planner is taking its turn
                await asyncio.sleep(2)
                continue
            kind, row_id = job["kind"], job["id"]
            unlimited = _owner_unlimited(kind, row_id)
            if _held["id"] and (not unlimited or kind == "voice_jobs"
                                or time.monotonic() - _held["since"] > LEASE_REUSE_S):
                await _drop_lease()
            if unlimited and _held["id"] and not await wis.renew_gpu(
                    _held["id"], LEASE_HOLD_S.get(kind, 900)):
                _held["id"], _held["since"] = None, 0.0     # it ended; take a new one
                _remember(None)
            if unlimited:
                if not _held["id"]:
                    _set(kind, row_id, message="taking a turn on the GPU")
                    try:
                        lease = await wis.lease_gpu(f"studio-{kind}",
                                                    LEASE_HOLD_S.get(kind, 900))
                    except Exception as exc:                   # noqa: BLE001
                        # WIS down or refusing: nothing of WIS's can be on the card
                        # for long, so run; the engine's own OOM answer still requeues.
                        log.warning("no GPU lease (%s); running without one", exc)
                        lease = ""
                    if lease is None:
                        continue          # WIS said busy; ask again, no timeout
                    _held["id"], _held["since"] = lease or None, time.monotonic()
                    _remember(_held["id"])
                ready, why = True, ""
            else:
                ready, why = await _gpu_ready(kind, _need_mb(kind, row_id, job["job_kind"]))
            if not ready:
                if db.now() - job["created_at"] > (job["wait_limit_s"] or MAX_WAIT_S):
                    _set(kind, row_id, status="failed",
                         error=f"the GPU stayed busy for {MAX_WAIT_S // 60} minutes; "
                               "please try again later")
                else:
                    _set(kind, row_id, message=why)
                    await asyncio.sleep(POLL_S)
                continue
            row = db.one(f"SELECT * FROM {kind} WHERE id=?", (row_id,))
            _set(kind, row_id, status="generating", message="generating")
            _running["n"] += 1
            try:
                await _handlers[kind](row)
            except Busy:
                attempts = (row["attempts"] or 0) + 1
                if attempts >= MAX_ATTEMPTS and not unlimited:
                    _set(kind, row_id, status="failed", attempts=attempts,
                         error="the GPU kept running out of memory; please try again later")
                else:
                    log.info("%s %s hit a full GPU; requeued (%d)", kind, row_id, attempts)
                    _set(kind, row_id, status="queued", attempts=attempts,
                         message="the GPU filled up; waiting to retry")
                    if unlimited:
                        await _drop_lease()   # hand the card back, then queue again
                    # Back off: an engine that keeps answering busy (or is unreachable)
                    # must not make WIS unload and reload its models every 2 s. On
                    # 2026-11-23 a test container with no route to the speech engine
                    # took 269 leases in 25 minutes this way.
                    await asyncio.sleep(min(120, 2 * 2 ** min(attempts, 6)) if unlimited else 20)
            except Exception as exc:                           # noqa: BLE001
                log.exception("%s %s failed", kind, row_id)
                _set(kind, row_id, status="failed", error=(str(exc) or type(exc).__name__)[:500])
            finally:
                _running["n"] -= 1
                if kind == "voice_jobs":
                    await _drop_lease()
        except asyncio.CancelledError:
            raise
        except Exception:                                      # noqa: BLE001
            log.exception("gpu queue survived an error")
            await asyncio.sleep(POLL_S)
