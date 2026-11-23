"""The style engine: ACE-Step's API server for singing-style (LoKr) training, and for
rendering songs with a person's style adapter. Separate from the `music` service.

Why a separate engine, and why it exits so eagerly (measured 2026-10-25, see
docs/VOICE_API_SPEC.md §10.6):

- Training must run with CPU offload OFF. With offload on, preprocessing fails
  ("tensors on cuda:0 / cpu" in build_context_latents), and the live engine needs
  offload to stay small. Without offload, the loaded model sits on the GPU, so this
  process must not linger after its job.
- Training leaves the LoKr hooks on the live decoder, with no restore(), so every
  later song would silently use a half-trained adapter. Ending the process is the
  clean reset; `restart: unless-stopped` brings it back lazy, holding nothing.
- Training, preprocessing and auto-labelling are threads, not jobs, so the
  /v1/stats job counts do not show them; they are checked separately here.

So: once models are loaded and nothing (job, training, preprocessing) has
happened for STUDIO_STYLE_IDLE_EXIT_S, the process exits.
"""
import json
import os
import threading
import time
import urllib.request

IDLE_EXIT_S = int(os.getenv("STUDIO_STYLE_IDLE_EXIT_S", "45"))
PORT = int(os.getenv("ACESTEP_API_PORT", "8001"))


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=10) as resp:
        return json.load(resp).get("data") or {}


def _busy() -> tuple[bool, bool]:
    """(busy, models_loaded)."""
    health, stats = _get("/health"), _get("/v1/stats")
    jobs = stats.get("jobs") or {}
    busy = bool(jobs.get("running") or jobs.get("queued") or stats.get("queue_size"))
    try:
        busy = busy or bool(_get("/v1/training/status").get("is_training"))
    except Exception:                                          # noqa: BLE001
        pass
    for path in ("/v1/dataset/preprocess_status", "/v1/dataset/auto_label_status"):
        try:
            busy = busy or _get(path).get("status") == "running"
        except Exception:                                      # noqa: BLE001
            pass
    loaded = bool(health.get("models_initialized") or health.get("llm_initialized"))
    return busy, loaded


def _watch() -> None:
    last_busy = time.time()
    while True:
        time.sleep(5)
        try:
            busy, loaded = _busy()
        except Exception:                                      # noqa: BLE001
            continue                       # still starting, or briefly unresponsive
        if busy or not loaded:
            last_busy = time.time()
            continue
        if time.time() - last_busy > IDLE_EXIT_S:
            print(f"[studio] style engine idle {IDLE_EXIT_S}s with a model on the GPU; "
                  "exiting to free the card and drop any adapter", flush=True)
            os._exit(0)


if __name__ == "__main__":
    threading.Thread(target=_watch, daemon=True).start()
    from acestep.api_server import main
    main()
