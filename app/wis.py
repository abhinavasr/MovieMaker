"""Client for the WIS video API.

Deliberately thin. Every field name here is one the service's own contract
(`docs/spec.md` 8b) uses, because the failure this endpoint is famous for is a
caller inventing a plausible field: a consumer sent its starting image as
`reference_images` for two days and every job silently generated from the
prompt alone. So: no aliases, no "helpfully" renamed keys, and any
`service_notes` the service returns are surfaced to the operator rather than
dropped.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field

import httpx

from app.config import cfg

log = logging.getLogger("studio.wis")

TERMINAL = {"succeeded", "failed", "cancelled"}


class WisError(RuntimeError):
    def __init__(self, message: str, *, code: str = "error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass
class Job:
    id: str
    status: str
    progress: int = 0
    message: str = ""
    artifacts: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.status in TERMINAL


def _headers() -> dict:
    return {"Authorization": f"Bearer {cfg.WIS_API_KEY}"}


def _job_from(body: dict) -> Job:
    return Job(
        id=body.get("id", ""),
        status=body.get("status", "unknown"),
        progress=int(body.get("progress") or 0),
        message=body.get("message") or "",
        artifacts=body.get("artifacts") or [],
        notes=body.get("service_notes") or [],
        raw=body,
    )


def _code(resp: httpx.Response) -> str:
    try:
        return (resp.json().get("error") or {}).get("code") or ""
    except ValueError:
        return ""


def _message(resp: httpx.Response) -> str:
    try:
        return (resp.json().get("error") or {}).get("message") or ""
    except ValueError:
        return resp.text[:200]


def _raise(resp: httpx.Response) -> None:
    try:
        err = resp.json().get("error") or {}
    except ValueError:
        raise WisError(f"WIS returned HTTP {resp.status_code}: {resp.text[:300]}")
    raise WisError(err.get("message") or f"HTTP {resp.status_code}",
                   code=err.get("code") or "error",
                   retryable=bool(err.get("retryable")))


async def health() -> dict:
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{cfg.WIS_BASE_URL}/v1/health", headers=_headers())
    if resp.status_code >= 400:
        _raise(resp)
    return resp.json()


async def lease_gpu(label: str, hold_seconds: int) -> str | None:
    """Take a turn on the card in WIS's own queue (WIS unloads its models for it).

    Waits as long as WIS takes to reach it (up to ~15 min per call). None when WIS
    answered busy; the caller asks again. Raises on a hard error.
    """
    async with httpx.AsyncClient(timeout=1000.0) as client:
        resp = await client.post(f"{cfg.WIS_BASE_URL}/v1/gpu/lease", headers=_headers(),
                                 json={"label": label, "hold_seconds": int(hold_seconds)})
    if resp.status_code == 503:
        return None
    if resp.status_code >= 400:
        _raise(resp)
    lease_id = resp.json()["lease_id"]
    # Confirm at once: an unconfirmed lease ends after WIS's short grace, which is
    # what frees the card if this process died while waiting for it.
    if not await renew_gpu(lease_id, hold_seconds):
        return None
    return lease_id


async def renew_gpu(lease_id: str, hold_seconds: int) -> bool:
    """Extend a held lease to end hold_seconds from now. False if it has ended."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(f"{cfg.WIS_BASE_URL}/v1/gpu/lease/{lease_id}/renew",
                                     headers=_headers(),
                                     json={"hold_seconds": int(hold_seconds)})
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


async def release_gpu(lease_id: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            await client.delete(f"{cfg.WIS_BASE_URL}/v1/gpu/lease/{lease_id}",
                                headers=_headers())
    except httpx.HTTPError:
        pass   # WIS ends it at its hold time anyway


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def _post_retrying_connect(url: str, payload: dict) -> httpx.Response:
    """POST, retrying only while the connection is REFUSED (the request never left,
    so retrying cannot submit twice) -- i.e. while WIS restarts. Up to ~3 minutes."""
    for attempt in range(36):
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                return await client.post(url, headers=_headers(), json=payload)
        except httpx.ConnectError:
            if attempt == 35:
                raise
            await asyncio.sleep(5)


async def submit_video(payload: dict) -> Job:
    """POST /v1/videos. Returns the accepted job, notes included.

    WIS answers 503 `unavailable` while its GPU worker is not reporting, e.g. for
    the first seconds after a reboot. On 2026-11-22 that failed 8 queued renders
    in one second; it is a wait, not a failure, so wait up to 10 minutes."""
    for _ in range(60):
        resp = await _post_retrying_connect(f"{cfg.WIS_BASE_URL}/v1/videos", payload)
        if resp.status_code != 503 or _code(resp) != "unavailable":
            break
        log.info("WIS is not ready yet (%s); waiting", _message(resp))
        await asyncio.sleep(10)
    if resp.status_code >= 400:
        _raise(resp)
    job = _job_from(resp.json())
    for note in job.notes:
        log.warning("WIS service_note on %s: %s", job.id, note)
    return job


async def submit_image(payload: dict) -> Job:
    """POST /v1/images (spec section 3). Image-to-image is `reference_images`
    here -- on this route that IS the field, unlike /v1/videos."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{cfg.WIS_BASE_URL}/v1/images",
                                 headers=_headers(), json=payload)
    if resp.status_code >= 400:
        _raise(resp)
    job = _job_from(resp.json())
    for note in job.notes:
        log.warning("WIS service_note on %s: %s", job.id, note)
    return job


# `route` is "videos" or "images": the two job kinds live under separate paths
# and an id from one is a 404 on the other.
async def get_job(job_id: str, route: str = "videos") -> Job:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{cfg.WIS_BASE_URL}/v1/{route}/{job_id}", headers=_headers())
    if resp.status_code >= 400:
        _raise(resp)
    return _job_from(resp.json())


async def cancel_job(job_id: str, route: str = "videos") -> None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.delete(f"{cfg.WIS_BASE_URL}/v1/{route}/{job_id}", headers=_headers())


async def wait_for(job_id: str, on_progress=None, poll_seconds: float = 5.0) -> Job:
    """Poll until terminal.

    Polling, not `?wait=true`: the contract caps that at 90 s because the
    Cloudflare edge cuts a request at ~100 s, and a clip here takes minutes.
    """
    down_since = None
    while True:
        try:
            job = await get_job(job_id)
            down_since = None
        except httpx.TransportError:
            # WIS restarting is not the clip failing: its job keeps running and is
            # still there when the API comes back. A render used to fail on the
            # first refused poll (three did on 2026-11-21 during a 10 s restart).
            loop = asyncio.get_running_loop()
            down_since = down_since or loop.time()
            if loop.time() - down_since > 600:
                raise
            await asyncio.sleep(poll_seconds)
            continue
        if on_progress:
            await on_progress(job)
        if job.done:
            return job
        await asyncio.sleep(poll_seconds)


async def download_artifact(job: Job) -> bytes:
    if not job.artifacts:
        raise WisError(f"job {job.id} succeeded with no artifact")
    url = job.artifacts[0]["url"]
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.get(f"{cfg.WIS_BASE_URL}{url}", headers=_headers())
    if resp.status_code >= 400:
        _raise(resp)
    return resp.content


async def text_json(prompt: str, *, max_tokens: int = 1200,
                    temperature: float = 0.7) -> dict:
    """POST /v1/text/json -- strict JSON out of the host's Ollama.

    Optional on the service: it is only configured when `WIS_TEXT_MODEL` is
    set, so every caller here treats a failure as "no auto-planning today"
    rather than as a broken render.
    """
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(
            f"{cfg.WIS_BASE_URL}/v1/text/json", headers=_headers(),
            json={"prompt": prompt, "max_tokens": max_tokens,
                  "temperature": temperature})
    if resp.status_code >= 400:
        _raise(resp)
    return resp.json().get("json") or {}
