"""Run the ACE-Step API server, and exit it after a stretch of idleness.

With CPU offload, ACE-Step parks every model it has loaded in host RAM: measured
34 GB for XL turbo + XL base + the 4B LM. This box has 62 GB and WIS needs up to
~32 GB for a model load, so holding that permanently would push the host into
swap. There is no unload endpoint, so the process simply ends once it has been
idle for STUDIO_MUSIC_IDLE_EXIT_S with models loaded; `restart: unless-stopped`
brings it straight back in its lazy state (~1-2 GB), and the next song loads
from disk again (~30 s). The studio's GPU queue retries a job that meets the
engine mid-restart, so nothing is lost.
"""
import json
import os
import threading
import time
import urllib.request

IDLE_EXIT_S = int(os.getenv("STUDIO_MUSIC_IDLE_EXIT_S", "600"))
PORT = int(os.getenv("ACESTEP_API_PORT", "8001"))


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=10) as resp:
        return json.load(resp).get("data") or {}


def _watch() -> None:
    last_total, last_busy = None, time.time()
    while True:
        time.sleep(30)
        try:
            health, stats = _get("/health"), _get("/v1/stats")
        except Exception:                                      # noqa: BLE001
            continue                       # still starting, or briefly unresponsive
        jobs = stats.get("jobs") or {}
        busy = jobs.get("running", 0) or jobs.get("queued", 0) or stats.get("queue_size", 0)
        if busy or jobs.get("total") != last_total:
            last_total, last_busy = jobs.get("total"), time.time()
            continue
        loaded = health.get("models_initialized") or health.get("llm_initialized")
        if loaded and time.time() - last_busy > IDLE_EXIT_S:
            print(f"[studio] idle {IDLE_EXIT_S}s with models in RAM; exiting to free it",
                  flush=True)
            os._exit(0)


if __name__ == "__main__":
    if IDLE_EXIT_S > 0:
        threading.Thread(target=_watch, daemon=True).start()
    from acestep.api_server import main
    main()
