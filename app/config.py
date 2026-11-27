"""Configuration, all of it from the environment.

Nothing here has a secret as its default. `STUDIO_ADMIN_PASSWORD` with no value
is a hard failure at import rather than a box that anyone who finds the URL can
walk into -- this service is meant to sit on a public hostname, so a default
credential is not a convenience, it is the whole attack.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path


def _s(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


class Config:
    # --- upstream ---
    # The WIS container on the shared `wis_default` network. Not 127.0.0.1:8090:
    # that bind is host-local, and from inside a container it reaches nothing.
    WIS_BASE_URL = _s("STUDIO_WIS_URL", "http://wis-api:8090").rstrip("/")
    WIS_API_KEY = _s("STUDIO_WIS_KEY")

    # --- models ---
    # Which WIS model backs each mode. Looked up on /v1/health at startup so a
    # renamed model is a clear error on the status page rather than a 400 that
    # only appears once somebody has waited in a queue.
    STORYBOARD_MODEL = _s("STUDIO_STORYBOARD_MODEL", "minimax-h3-i2v")
    SINGLE_MODEL = _s("STUDIO_SINGLE_MODEL", "minimax-h3-ref2v")
    # Pictures. FLUX.2 klein does text-to-image and reference editing in one
    # model at 4 steps (~12-25 s), and takes several source pictures at once.
    IMAGE_MODEL = _s("STUDIO_IMAGE_MODEL", "flux-2-klein-9b")
    # Speech. The studio's own Qwen3-TTS container (`tts` in compose): preset
    # speakers via CustomVoice, cloning via Base. Not WIS, so no WIS key bucket.
    TTS_URL = _s("STUDIO_TTS_URL", "http://tts:8097").rstrip("/")
    # Burmese speech: VoxCPM2 in its own container (`tts-my` in compose), same contract.
    TTS_MY_URL = _s("STUDIO_TTS_MY_URL", "http://tts-my:8098").rstrip("/")
    # Sanskrit śloka chant: the Vāgdhenu API (~/vagdhenu, its own stack), reached over
    # its compose network as an external network. Its key is the stack's own.
    VAGDHENU_URL = _s("STUDIO_VAGDHENU_URL", "http://vagdhenu-tts:8095").rstrip("/")
    VAGDHENU_KEY = _s("STUDIO_VAGDHENU_KEY", "")
    # Music. The studio's own ACE-Step 1.5 container (`music` in compose).
    MUSIC_URL = _s("STUDIO_MUSIC_URL", "http://music:8001").rstrip("/")
    RVC_URL = _s("STUDIO_RVC_URL", "http://rvc:8098").rstrip("/")
    STYLE_URL = _s("STUDIO_STYLE_URL", "http://music-train:8001").rstrip("/")
    TRAINING_DIR = Path(_s("STUDIO_TRAINING_DIR", "/training"))
    # Hours (UTC) style training may START, as "lo-hi". Default any time: the box is
    # dedicated to us and is usually off at night (user, 2026-10-25). The job still
    # waits for WIS to be idle and 21 GB of the card to be free.
    STYLE_WINDOW = tuple(int(x) for x in _s("STUDIO_STYLE_TRAIN_WINDOW", "0-24").split("-"))
    STYLE_MAX_EPOCHS = _i("STUDIO_STYLE_MAX_EPOCHS", 300)

    # --- assistant (app/agent.py) ---
    # The host's Ollama. The planner is the strongest local model and runs on the
    # GPU only while it holds a WIS GPU lease; the executor is a small model run
    # on the CPU (num_gpu 0) so it never competes with renders for the card.
    OLLAMA_URL = _s("STUDIO_OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/")
    PLANNER_MODEL = _s("STUDIO_PLANNER_MODEL", "qwen3.8:27b")
    # Podcasts think on Qwen3.8-Flash-Next (llama.cpp via llama-swap, container `flashnext`):
    # better answers than the 27B in the 2026-11-27 test, ~1.5-2x slower -- fine for a
    # non-interactive pipeline; chats stay on the 27B. Empty = podcasts use the planner too.
    PODCAST_LLM_URL = _s("STUDIO_PODCAST_LLM_URL", "http://flashnext:8080").rstrip("/")
    PODCAST_LLM_MODEL = _s("STUDIO_PODCAST_LLM_MODEL", "flash-next")
    PLANNER_CTX = _i("STUDIO_PLANNER_CTX", 32768)
    EXECUTOR_MODEL = _s("STUDIO_EXECUTOR_MODEL", "qwen3.5:9b")
    EXECUTOR_CTX = _i("STUDIO_EXECUTOR_CTX", 16384)
    EXECUTOR_THREADS = _i("STUDIO_EXECUTOR_THREADS", 12)
    # Cheap status checks of delegated work are plain code, every POLL seconds.
    # The planner itself reviews when something finishes or fails, and at least
    # every REVIEW seconds while work is still running. Each review pauses the
    # GPU for about a minute, so this is a throughput trade-off.
    AGENT_POLL_S = _i("STUDIO_AGENT_POLL_S", 30)
    # 0 = no agent planner worker in this process (test copies: a test container's worker
    # once resumed an old test conversation and held the real GPU, 2026-11-24).
    AGENT_WORKER = _s("STUDIO_AGENT_WORKER", "1") not in ("0", "false", "no")
    AGENT_REVIEW_S = _i("STUDIO_AGENT_REVIEW_S", 600)
    # Review-driven correction rounds per request before it must ask the person.
    AGENT_MAX_CORRECTIONS = _i("STUDIO_AGENT_MAX_CORRECTIONS", 3)
    USER_AGENT_TURNS_PER_HOUR = _i("STUDIO_USER_AGENT_TURNS_PER_HOUR", 30)
    # Per attached file in the chat. Cloudflare refuses request bodies over 100 MB.
    AGENT_UPLOAD_MB = _i("STUDIO_AGENT_UPLOAD_MB", 95)

    # --- identity ---
    ADMIN_USERNAME = _s("STUDIO_ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = _s("STUDIO_ADMIN_PASSWORD")
    SESSION_TTL_HOURS = _i("STUDIO_SESSION_TTL_HOURS", 12)
    # Cookies are marked Secure by default because the intended front door is a
    # Cloudflare tunnel, i.e. https. Set to 0 only for a plain-http LAN test.
    COOKIE_SECURE = _s("STUDIO_COOKIE_SECURE", "1") not in ("0", "false", "no")
    # The address assistants are told to connect to, e.g. https://studio.example.com.
    # Blank means "whatever host the request arrived on", which is right behind
    # the tunnel and is what lets the same build be tested on 127.0.0.1.
    PUBLIC_URL = _s("STUDIO_PUBLIC_URL").rstrip("/")

    # --- Batiyao (batiyao.com): sign in with Batiyao, share to Batiyao ---
    # Register the studio at batiyao.com -> Settings -> Developer -> Register an app, with the
    # redirect URI <public url>/auth/batiyao/callback. The client ID is public (PKCE, no
    # secret). Empty = the Batiyao buttons are hidden.
    BATIYAO_CLIENT_ID = _s("STUDIO_BATIYAO_CLIENT_ID")
    BATIYAO_API = _s("STUDIO_BATIYAO_API", "https://api.batiyao.com").rstrip("/")
    # Fernet key that encrypts Batiyao tokens at rest; without it Batiyao stays off. Generate:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    BATIYAO_TOKEN_KEY = _s("STUDIO_BATIYAO_TOKEN_KEY")
    # A first Batiyao sign-in makes a studio account (the person's choice, 2026-11-25).
    BATIYAO_AUTO_ACCOUNTS = _s("STUDIO_BATIYAO_AUTO_ACCOUNTS", "1") not in ("0", "false", "no")

    # --- limits ---
    MAX_UPLOAD_MB = _i("STUDIO_MAX_UPLOAD_MB", 16)
    MAX_STORYBOARD_SHOTS = _i("STUDIO_MAX_STORYBOARD_SHOTS", 12)
    DEFAULT_SHOT_SECONDS = _i("STUDIO_DEFAULT_SHOT_SECONDS", 5)
    # Longest clip each mode's WIS model accepts. WIS turns seconds into frames at
    # 24 fps and rounds UP to its frame grid, so 12 s becomes 294 frames, one over
    # minimax-h3-i2v's 293: a storyboard shot tops out at 11 s (277 frames).
    # minimax-h3-ref2v takes 362 frames, exactly 15 s. Before 2026-11-22 both were
    # capped at 15 and a 13 s shot failed the whole render ("EP01 3D group A").
    STORYBOARD_MAX_SECONDS = _i("STUDIO_STORYBOARD_MAX_SECONDS", 11)
    SINGLE_MAX_SECONDS = _i("STUDIO_SINGLE_MAX_SECONDS", 15)
    # Lip-sync shots: Wan2.2-S2V through WIS (picture + the dialogue under the shot).
    LIPSYNC_MODEL = _s("STUDIO_LIPSYNC_MODEL", "wan2.2-s2v")
    # "Copy a video's motion" shots: the shot picture performs the reference video's moves.
    MOTION_MODEL = _s("STUDIO_MOTION_MODEL", "wan-animate-2")
    MOTION_MAX_SECONDS = 15
    # Two people talk / talk or sing at length: InfiniteTalk (one picture + 1-2 voices).
    TALK_MODEL = _s("STUDIO_TALK_MODEL", "infinitetalk")
    TALK_MAX_SECONDS = 60
    # Two-person InfiniteTalk shots send each character's own track on one timeline (WIS
    # speaker_mode "together"); off = the older one-then-the-other mode.
    TALK_TOGETHER = _s("STUDIO_TALK_TOGETHER", "1") not in ("0", "false", "no")
    # Recast the people in a video / change its look: Viggle Remake (swap / edit).
    RECAST_MODEL = _s("STUDIO_RECAST_MODEL", "viggle-remake-swap")
    RESTYLE_MODEL = _s("STUDIO_RESTYLE_MODEL", "viggle-remake-edit")
    REMAKE_MAX_SECONDS = 14
    # The local planner model rewrites every shot's video prompt before a render.
    PROMPT_POLISH = _s("STUDIO_PROMPT_POLISH", "1") not in ("0", "false", "off")
    LIPSYNC_MAX_SECONDS = _i("STUDIO_LIPSYNC_MAX_SECONDS", 30)   # measured: 30 s at 480x864 in 335 s
    # Per user, per rolling hour. The box has one GPU and a clip is ~2m15s of
    # it, so this is a real resource, not a formality.
    USER_RENDERS_PER_HOUR = _i("STUDIO_USER_RENDERS_PER_HOUR", 10)
    # Pictures are cheaper than clips, but every one is a job on the studio's
    # WIS key, and that key has ONE hourly bucket (60) shared by every user and
    # every storyboard shot. This keeps one person's pictures from spending it.
    USER_IMAGES_PER_HOUR = _i("STUDIO_USER_IMAGES_PER_HOUR", 20)
    USER_SPEECH_PER_HOUR = _i("STUDIO_USER_SPEECH_PER_HOUR", 30)
    USER_MUSIC_PER_HOUR = _i("STUDIO_USER_MUSIC_PER_HOUR", 12)
    # Podcasts: each runs ~15-25 planner calls and reads ~20 web pages.
    USER_PODCASTS_PER_DAY = _i("STUDIO_USER_PODCASTS_PER_DAY", 6)
    # How long a finished project's files stay on disk.
    RETENTION_DAYS = _i("STUDIO_RETENTION_DAYS", 90)

    # --- storage ---
    DATA_DIR = Path(_s("STUDIO_DATA_DIR", "/data"))

    @property
    def db_path(self) -> Path:
        return self.DATA_DIR / "studio.db"

    @property
    def uploads_dir(self) -> Path:
        return self.DATA_DIR / "uploads"

    @property
    def outputs_dir(self) -> Path:
        return self.DATA_DIR / "outputs"

    @property
    def images_dir(self) -> Path:
        return self.DATA_DIR / "images"

    @property
    def speech_dir(self) -> Path:
        return self.DATA_DIR / "speech"

    @property
    def music_dir(self) -> Path:
        return self.DATA_DIR / "music"

    @property
    def voices_dir(self) -> Path:
        return self.DATA_DIR / "voices"

    def validate(self) -> None:
        missing = []
        if not self.WIS_API_KEY:
            missing.append("STUDIO_WIS_KEY")
        if not self.ADMIN_PASSWORD:
            missing.append("STUDIO_ADMIN_PASSWORD")
        if missing:
            raise RuntimeError(
                "refusing to start without " + ", ".join(missing) +
                ". Set them in .env -- there is deliberately no default, because this "
                "service is designed to be published on a public hostname.")
        for d in (self.DATA_DIR, self.uploads_dir, self.outputs_dir, self.images_dir,
                  self.speech_dir, self.music_dir, self.voices_dir):
            d.mkdir(parents=True, exist_ok=True)


cfg = Config()


def new_token() -> str:
    return secrets.token_urlsafe(32)
