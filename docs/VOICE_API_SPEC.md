# Story Studio — Voice API specification

**Version:** 1.1 · **Date:** 2026-10-25 · **Status:** layers 1, 2 and 3 live (§5, §6, §7.1–7.3, §8 voice tools); §7.4–7.6, pagination, `Idempotency-Key`, webhooks (§4.3), `preview_url` and two-voice clip rejection are not built yet · **Owner:** Abhinava
**Base URL:** `https://studio.abhinava.xyz`

This document specifies the Voice features of Story Studio: training a personal voice from a
person's own recordings, and using it to sing, speak and re-sing songs, with that person's
singing style. It is written for whoever builds the application (web/mobile app, or the
backend itself). Read §1–§9 to build a client. Read §10 to build the backend.

Markers used throughout:

- **[exists]** — live today; documented here so the app can use it.
- **[new]** — to be built; this spec is the contract.

---

## Contents

1. [What the product does](#1-what-the-product-does)
2. [Conventions](#2-conventions)
3. [Authentication](#3-authentication)
4. [Jobs: the async model every endpoint uses](#4-jobs-the-async-model-every-endpoint-uses)
5. [Voices — training a personal voice (layers 1 + 2)](#5-voices--training-a-personal-voice-layers-1--2)
6. [Style models — learning how a person sings (layer 3)](#6-style-models--learning-how-a-person-sings-layer-3)
7. [Using a voice](#7-using-a-voice)
8. [MCP tools](#8-mcp-tools)
9. [Limits, errors, privacy](#9-limits-errors-privacy)
10. [Backend implementation notes](#10-backend-implementation-notes)
11. [Acceptance tests](#11-acceptance-tests)
12. [Open questions](#12-open-questions)

---

## 1. What the product does

A person uploads recordings of **their own voice** — speaking and, more importantly, singing.
The studio trains three things from them:

| Layer | Name in API | What it captures | Needs from the person | Built on |
|---|---|---|---|---|
| 1 | `voice` (model) | How their voice **sounds** (timbre), in the singing register | ≥ 5 min of singing (10–30 sounds fuller; speech optional) | RVC v2 |
| 2 | `voice.profile` | Their **range**, vibrato, tone (breathy/bright), typical dynamics | nothing extra — measured from layer-1 uploads | analysis |
| 3 | `style_model` | How they **sing** — phrasing, ornamentation, delivery | ≥ 3 full songs (8+ min) of them singing, with lyrics; 10–20 is better | ACE-Step 1.5 LoKr adapter |

Then the voice can be used to:

| Use | Endpoint | What happens |
|---|---|---|
| Change the singer | `POST /v1/voices/{id}/resing` | An existing song is separated, its lead vocal is re-sung in the voice — moved into the person's range — and mixed back onto the **original** music. |
| Sing a new song | `POST /v1/voices/{id}/songs` | A new song is composed and performed in the person's manner (profile + reference clip, plus style model if trained), then rendered in their exact voice. |
| Speak text | `POST /v1/voices/{id}/speech` | Text-to-speech, converted to their voice. |
| Video soundtracks | `POST /v1/projects/{id}/audio` | Any result above goes into a storyboard / single-clip video project. |

**Important honesty rule for the UI:** layer 1 alone changes *how it sounds*, not *how it is sung*.
Without layer 3, a re-sung song keeps the original singer's melody and phrasing. Layer 2 moves it into the
person's range and steers new songs toward their manner. The UI must not promise "sings exactly like you"
unless a style model is attached.

---

## 2. Conventions

- **Transport:** HTTPS only. JSON request and response bodies (`Content-Type: application/json`), except
  file uploads, which are `multipart/form-data`.
- **Prefix:** all new endpoints live under `/v1/`. The existing `/api/*` routes are the web page's private API
  (cookie-only) and are **not** for third-party apps.
- **IDs:** opaque strings with a type prefix: `voi_…` voice, `clip_…` clip, `sty_…` style model,
  `vjob_…` voice job, `mus_…` song, `spk_…` speech, `prj_…` project. Treat them as opaque.
- **Times:** Unix seconds (float) in JSON fields ending `_at`.
- **Durations:** seconds, as numbers, in fields ending `_s`.
- **Audio in:** mp3, wav, flac, m4a, ogg, webm, or a video file (its soundtrack is used). Anything is decoded
  server-side. Max **100 MB** per file; longer files are cut to the limits in §9.
- **Audio out:** mp3 (44.1 kHz stereo, 320 kbps) by default; `?format=wav` on any `audio_url` gives WAV.
- **Media links:** results carry **signed URLs** (`/a/<id>/<exp>/<sig>/<name>`), valid **7 days**, that work
  without auth (so they can be put in an `<audio>` tag or shared). Re-fetch the object for a fresh link.
- **Pagination:** list endpoints take `?limit=` (default 30, max 100) and `?before=<created_at>`; responses
  have `{"items": [...], "next_before": <float|null>}`.
- **Idempotency:** every `POST` that creates something accepts an `Idempotency-Key` header (any string ≤ 64
  chars). The same key within 24 h returns the original response instead of creating a duplicate.

---

## 3. Authentication

**[exists]** The studio is an OAuth 2.1 authorization server: authorization-code + PKCE, **device sign-in
(RFC 8628)**, dynamic client registration and refresh tokens. It is what the MCP server already uses. Accounts are issued by the
studio admin; there is no public sign-up.

| Endpoint | Purpose |
|---|---|
| `GET /.well-known/oauth-authorization-server` | Server metadata (RFC 8414) |
| `GET /.well-known/oauth-protected-resource` | Resource metadata (RFC 9728) |
| `POST /oauth/register` | Dynamic client registration (RFC 7591) |
| `GET /oauth/authorize` | Sign-in + consent page (user enters studio username/password) |
| `POST /oauth/token` | Code → tokens; refresh; device-code polling |
| `POST /oauth/device_authorization` | Device sign-in: get a `user_code` for the person to approve |
| `GET/POST /activate` | Device sign-in: the page where the person enters the code and approves |

**[exists]** Every `/v1/*` endpoint accepts `Authorization: Bearer <access_token>` from that same server.

- Scope: `studio` (the only scope; all endpoints).
- A disabled, expired or deleted account gets `401` on the next call (tokens are revoked server-side).
- A mobile app registers itself once with `redirect_uris: ["myapp://oauth"]` and uses PKCE (S256).
  It must not embed any secret.
- **A sandboxed mini app, TV or script** that can't redirect or open a pop-up uses **device sign-in**. It
  registers with `grant_types` containing `urn:ietf:params:oauth:grant-type:device_code` (no redirect needed),
  shows the person a code to approve at `/activate`, and polls `/oauth/token`. The full call sequence and
  answers are in the complete spec, §A.1.
- **Browser and mini apps (CORS):** `/v1/*`, `/mcp` and `/oauth/*` accept cross-origin calls from any origin,
  `Origin: null` included. Preflights get `204`. Cookies are never accepted cross-origin: send the bearer token.

---

## 4. Jobs: the async model every endpoint uses

Everything that touches the GPU is **asynchronous**. The single GPU (RTX 4090, 24 GB) is shared with
the studio's picture/video service, songs and speech. Work waits in **one queue**. A create call returns
immediately with a job; the client polls, or registers a webhook.

### 4.1 Job object

```json
{
  "id": "vjob_8f2c…",
  "kind": "resing",                 // see table below
  "status": "queued",               // queued | running | succeeded | failed | cancelled
  "message": "waiting for pictures/videos ahead of it to finish",
  "queue_position": 3,              // 1 = next; null unless queued
  "progress": 0.0,                  // 0..1, best effort; null when unknown
  "stage": "separating",            // human-readable step, see each endpoint
  "eta_s": 95,                      // best-effort estimate; null when unknown
  "error": null,                    // plain-English reason when failed
  "created_at": 1792950000.1,
  "updated_at": 1792950012.4,
  "result": null,                   // filled when succeeded; shape per kind
  "voice_id": "voi_…"
}
```

| `kind` | Created by | `result` shape |
|---|---|---|
| `voice_training` | `POST /v1/voices/{id}/train` | `{"voice": Voice}` |
| `style_training` | `POST /v1/style-models/{id}/train` | `{"style_model": StyleModel}` |
| `resing` | `POST /v1/voices/{id}/resing` | `Audio` + `stems` |
| `song` | `POST /v1/voices/{id}/songs` | `Audio` + `lyrics`, `style`, `bpm`, `key` |
| `speech` | `POST /v1/voices/{id}/speech` | `Audio` |
| `transcribe` | `POST /v1/style-models/{id}/songs` (auto) | `{"lyrics": "…"}` |

`Audio`:
```json
{ "audio_url": "https://studio.abhinava.xyz/a/…/song.mp3", "duration_s": 42.3,
  "expires_at": 1793554800.0 }
```

### 4.2 Endpoints

| Method & path | Description |
|---|---|
| `GET /v1/jobs/{job_id}` | One job. Poll every **5 s** while `running`, every **20 s** while `queued`. |
| `GET /v1/jobs?status=&kind=&voice_id=` | The caller's jobs, newest first. |
| `POST /v1/jobs/{job_id}/cancel` | Cancel. `queued` → `cancelled` at once. `running` stops at the next safe point (training stops after the current slice and keeps its progress). Returns the job. |

### 4.3 Webhooks (optional)

Any create call may include `"callback_url": "https://…"`. On every status change the server POSTs the
job object there. The body is signed: header `X-Studio-Signature: t=<unix>,v1=<hex HMAC-SHA256 of
"<t>.<body>">`, keyed by a per-client webhook secret **[new]**, returned as `webhook_secret` by
`POST /oauth/register`. Expect retries
(1 m, 5 m, 30 m); respond 2xx within 10 s. Polling must still work — webhooks are a convenience.

### 4.4 Typical times (measured on this GPU when it is free)

| Job | Time |
|---|---|
| Re-sing a 3-min song | ~60–90 s |
| Speech, 1 paragraph | ~10–20 s |
| New 40 s song in a voice | ~80–120 s |
| Voice training, 15 min of audio | ~20–40 min of GPU, in ~5-min slices (wall-clock longer when busy) |
| Style model (LoKr), 15 songs | ~30–90 min of GPU, in slices |

When the card is busy, a job's wait is unbounded up to **45 min** for generation jobs (then
`failed` with a "GPU stayed busy" error) and **72 h** for training jobs.

---

## 5. Voices — training a personal voice (layers 1 + 2)

### 5.1 Lifecycle

```
 create ──► collecting ──(upload clips, ≥ minimum)──► ready_to_train ──train──► training
                ▲                                                            │
                └────────────── add more clips / retrain ◄── ready ◄─────────┘
                                                       (failed ◄─┘)
```

| `status` | Meaning |
|---|---|
| `collecting` | Accepting clips; minimum not met yet. |
| `ready_to_train` | Enough usable audio; `train` allowed. |
| `training` | A `voice_training` job is queued or running. |
| `ready` | Model trained; usable by every §7 endpoint. |
| `failed` | Last training failed; `last_error` says why; clips are kept; `train` again allowed. |

### 5.2 Voice object

```json
{
  "id": "voi_3a91…",
  "name": "My voice",
  "status": "ready",
  "created_at": 1792950000.0,
  "consent": {                       // see §9.3
    "attested": true,
    "statement_version": "2026-10-25",
    "attested_at": 1792950000.0
  },
  "dataset": {
    "clips": 9,
    "total_s": 1260.4,               // after cleaning
    "singing_s": 840.2,
    "speech_s": 420.2,
    "usable": true,
    "warnings": ["2 clips have background music; vocals were separated automatically"]
  },
  "training": {
    "epochs_done": 300, "epochs_target": 300,
    "slices_done": 7,
    "trained_at": 1792960000.0,
    "quality": "good"                // good | fair | poor — from held-out loss, see §10.4
  },
  "profile": Profile,                // null until trained (§5.6)
  "style_model_id": "sty_…",         // null if none attached (§6)
  "preview_url": "https://…/preview.mp3",  // a fixed test phrase sung in the voice
  "last_error": null
}
```

### 5.3 Endpoints

| Method & path | Body | Returns | Notes |
|---|---|---|---|
| `POST /v1/voices` | `{"name": str, "consent": {"attest": true, "statement_version": "2026-10-25"}}` | `201` Voice | `consent.attest` must be `true` or `400`. Max **3** voices per account. |
| `GET /v1/voices` | – | list of Voice | Only the caller's. |
| `GET /v1/voices/{id}` | – | Voice | |
| `PATCH /v1/voices/{id}` | `{"name": str}` | Voice | |
| `DELETE /v1/voices/{id}` | – | `204` | Deletes clips, model, profile, style model and all intermediate files. Results already made (songs/speech) are kept. Irreversible. |
| `POST /v1/voices/{id}/clips` | multipart: `file`, `kind` = `singing` \| `speech` \| `auto` (default `auto`), optional `lyrics` (str) | `201` Clip | Upload one recording. Analysed synchronously on the CPU (≤ 30 s for a 5-min clip). |
| `GET /v1/voices/{id}/clips` | – | list of Clip | |
| `DELETE /v1/voices/{id}/clips/{clip_id}` | – | `204` | Not allowed while `training`. |
| `GET /v1/voices/{id}/clips/{clip_id}/audio` | – | audio | The cleaned clip as it will be trained on, for the person to check. |
| `POST /v1/voices/{id}/train` | `{"quality": "standard" \| "high"}` (default `standard`) | `202` Job (`voice_training`) | Allowed in `ready_to_train`, `ready`, `failed`. Retraining replaces the model when it succeeds. |
| `GET /v1/voices/{id}/profile` | – | Profile | `404` until trained. |

### 5.4 Clip object

```json
{
  "id": "clip_77b0…",
  "name": "chorus-take-2.m4a",
  "kind": "singing",                // detected or given
  "duration_s": 182.0,              // original
  "usable_s": 164.5,                // after removing silence, music, noise
  "status": "ok",                   // ok | rejected
  "checks": {
    "music_removed": true,          // accompaniment was separated out
    "reverb_removed": false,
    "noise_db": -52.1,              // noise floor; < -45 is good
    "clipping": false,
    "single_speaker": true
  },
  "warnings": [],
  "rejected_reason": null           // e.g. "more than one voice in this recording"
}
```

**Clip rules the server enforces (the app should explain them before upload):**

- Only the uploader's own voice. One person per clip. A recording with two voices is **rejected**.
- Background music is fine — it is separated out automatically — but clean a-cappella is best.
- Each clip: 5 s – 10 min. Per voice: up to 60 min total; up to 40 clips.
- **Minimum to train:** **5 min usable singing** (`singing_s ≥ 300`); under 10 min the voice trains with a warning that more songs sound fuller. Duplicating clips does not help: it is the same as training longer. Speech-only voices are allowed but
  flagged `"warnings": ["trained mostly on speech — singing will sound thin"]`, and their minimum is 10 min
  of usable speech.
- Recommended: 15–30 min, covering the person's low and high notes, soft and loud.

### 5.5 Recording guidance (show this in the app)

- Quiet room, no echo; phone 20–30 cm from the mouth; no auto-tune or effects.
- Sing songs you know well, across your range: low, middle, high, soft and loud.
- 10 short songs beat one long one. Include some speaking if you'll use speech.

### 5.6 Profile object (layer 2)

Measured from the training clips when training finishes. Read-only.

```json
{
  "range": {
    "low_hz": 174.6,  "low_note": "F3",     // 5th percentile of sung pitch
    "mid_hz": 261.6,  "mid_note": "C4",     // median
    "high_hz": 523.3, "high_note": "C5",    // 95th percentile
    "comfortable": ["A3", "A4"]             // 25th–75th percentile
  },
  "voice_type": "alto",                     // bass | baritone | tenor | alto | mezzo | soprano (by range)
  "vibrato": { "rate_hz": 5.6, "extent_cents": 38, "share_of_long_notes": 0.62 },
  "tone": { "breathiness": 0.41, "brightness": 0.57, "dynamics_db": 14.2 },  // 0..1 scales
  "style_caption": "warm breathy alto, light quick vibrato on held notes, gentle dynamics",
  "reference_clip_id": "clip_77b0…"         // best clean singing excerpt; used to steer new songs
}
```

---

## 6. Style models — learning how a person sings (layer 3)

A style model is a small adapter for the song model (ACE-Step 1.5, LoKr) trained on **full songs the person
sang, with their lyrics**. With it attached, new songs are *performed* with their phrasing and
ornamentation before the voice model gives them their exact sound.

It belongs to one voice. Creating a style model does not change the voice.

### 6.1 Lifecycle

`collecting` → (≥ **3** songs with confirmed lyrics and ≥ **8 minutes**; 10–20 songs recommended) → `ready_to_train` → `training` → `ready` | `failed`

**As built:** training runs **whole, not in slices**. It starts as soon as WIS is idle and 21 GB of the card is free, at any hour (the box is dedicated; `STUDIO_STYLE_TRAIN_WINDOW` can restrict start hours if ever needed), on the separate `music-train` engine. Epochs are sized to a ~90-minute run, from 80 to 300. A `style_train` job may wait up to 72 h. Drafted lyrics come from Whisper on the CPU; a draft that loops ("hmm, hmm…") is discarded and the person is asked to type the words. The trained adapter (~15 MB) is kept; the uploaded songs expire after 90 days.

### 6.2 StyleModel object

```json
{
  "id": "sty_51de…",
  "voice_id": "voi_3a91…",
  "status": "ready",
  "songs": 14,
  "songs_with_lyrics": 14,
  "total_s": 2710.0,
  "training": { "epochs_done": 500, "epochs_target": 500, "trained_at": 1793000000.0 },
  "strength_default": 0.8,
  "last_error": null
}
```

### 6.3 Endpoints

| Method & path | Body | Returns | Notes |
|---|---|---|---|
| `POST /v1/voices/{voice_id}/style-model` | `{}` | `201` StyleModel | One per voice. Voice must exist; it need not be trained yet. |
| `GET /v1/style-models/{id}` | – | StyleModel | |
| `DELETE /v1/style-models/{id}` | – | `204` | Detaches from the voice and deletes everything. |
| `POST /v1/style-models/{id}/songs` | multipart: `file`, optional `lyrics`, optional `title` | `201` StyleSong | A full song the person sang (with or without music). If `lyrics` is missing, a `transcribe` job drafts them; the song is `needs_review` until confirmed. |
| `GET /v1/style-models/{id}/songs` | – | list of StyleSong | |
| `PATCH /v1/style-models/{id}/songs/{song_id}` | `{"lyrics": str, "confirmed": true}` | StyleSong | The person corrects and confirms the lyrics. |
| `DELETE /v1/style-models/{id}/songs/{song_id}` | – | `204` | |
| `POST /v1/style-models/{id}/train` | `{}` | `202` Job (`style_training`) | Needs ≥ 10 confirmed songs. |

StyleSong:
```json
{ "id": "ssong_…", "title": "Tum Hi Ho", "duration_s": 241.0,
  "lyrics": "[Verse]\n…", "lyrics_source": "transcribed",   // given | transcribed
  "status": "needs_review",                                   // needs_review | ok | rejected
  "language": "hi", "bpm": 94, "key": "F minor", "caption": "…" }   // caption/bpm/key auto-labelled
```

Rules: 3–40 songs, each 30 s – 6 min (only the first 240 s of each is used), solo lead vocal by the person (backing music fine). Lyrics must be
confirmed by the person — wrong lyrics teach wrong pronunciation.

---

## 7. Using a voice

Every endpoint in §7 requires the voice's `status` to be `ready` and the caller to **own** it (`404`
otherwise — never `403`, so voice IDs don't leak). Each creates a Job (§4). Each counts toward the hourly
limits in §9.

### 7.1 Change the singer — `POST /v1/voices/{id}/resing`

Re-sing an existing song's lead vocal in the voice and mix it back on the original music.

**As built:** `multipart/form-data` with these fields:

| Field | Value |
|---|---|
| `file` **or** `music_id` | the song: an upload (≤ 100 MB), or a studio song id |
| `key` | `auto` (default), or whole semitones `-24..24` |
| `keep_backing_vocals` | `true` (default) / `false` |
| `reverb` | `0..1`, default `0.2` |
| `vocal_db`, `music_db`, `backing_db` | optional whole-dB gains |
| `title` | optional |

(`url`/`upload_id` sources and `style_strength` are planned; MCP `resing_song` already accepts
`source_url`, `source_file`, `source_base64`.)

- `key: "auto"` moves the **converted vocal only, by whole octaves**, toward the voice's
  `profile.range.mid_hz`: no move when the original singer's median is within 5 semitones, otherwise the
  nearest whole octave (at least one). The music never changes key. Measured: converting a male lead at its
  own pitch still sounded more like the original singer; one octave up made it sound like the trained voice.
  An integer `key` that is a multiple of 12 moves the vocal only; any other integer moves the whole song
  (vocal, music and backing). The applied shift is reported as `key_shift_semitones`.
- `style_strength > 0` (needs a style model): the lead line is first re-performed by the song model with the
  style adapter, then converted. Melody changes slightly toward the person's phrasing; the music is
  untouched. `0` = pure voice swap.
- Stages: `fetching` → `separating` → `converting` → `mixing`.

`result`:
```json
{ "audio_url": "…/resing.mp3", "duration_s": 213.4, "expires_at": …,
  "key_shift_semitones": 12,
  "stems": { "vocal_url": "…", "music_url": "…", "backing_url": "…" } }
```

### 7.2 Sing a new song — `POST /v1/voices/{id}/songs`

```json
{
  "description": "a gentle monsoon love song in Hindi",   // EITHER description …
  "lyrics": "[Verse]\n…", "style": "acoustic pop, …",    // … OR lyrics (+ optional style)
  "language": "hi",                                       // optional; default auto
  "duration_s": 60,                                       // 10..360, optional
  "bpm": 90, "key": "A minor",                            // optional
  "use_style_model": true,                                // default true if one is ready
  "style_strength": 0.8,                                  // 0..1, adapter scale
  "seed": 1234,                                           // optional
  "callback_url": "optional"
}
```

What the server does (the app needn't care, but it explains the stages):
`composing` (writes lyrics if only a description was given) → `performing` (song model; the voice's
`style_caption` is merged into the style, its reference clip steers timbre, the style adapter is
applied when used, and the key is chosen to fit the voice's range) → `separating` → `converting`
(voice model) → `mixing`.

`result`: `Audio` + `{"lyrics", "style", "bpm", "key", "seed", "stems": {...}}`.

### 7.3 Speak text — `POST /v1/voices/{id}/speech`

```json
{ "text": "Once upon a time…",                 // 1..2000 chars
  "language": "en",                             // optional; default auto
  "delivery": "calm and slow",                  // optional speaking-style hint
  "callback_url": "optional" }
```

Stages: `speaking` → `converting`. `result`: `Audio`.

### 7.4 Re-sing / convert a short vocal — `POST /v1/voices/{id}/convert`

For apps that already have a dry vocal (e.g. the person hums a melody, or another singer's a-cappella).
No separation and no mixing.

```json
{ "source": { "upload_id": "upl_…" }, "key": "auto", "content": "singing" }  // singing | speech
```

### 7.5 Put a result in a video project — `POST /v1/projects/{project_id}/audio`

**[new; uses the existing project model]**

```json
{ "job_id": "vjob_…" }   // or "music_id" / "speech_id" [exists]
```

Returns `{"project_id": "prj_…", "files": n}`. In a storyboard it becomes the soundtrack. In single-clip
mode it becomes a reference audio (the clip model takes up to 3).

### 7.6 Uploads — `POST /v1/uploads`

multipart `file` → `201 {"upload_id": "upl_…", "duration_s": 213.4, "expires_at": …}`. Kept 24 h. Use
for any `source.upload_id`. Up to 100 MB.

### 7.7 Existing endpoints a voice app will also want **[exists, via MCP today; to be mirrored at `/v1`]**

| Capability | MCP tool | Mirror at |
|---|---|---|
| Make a song (describe / lyrics / cover / repaint / extract / add_layer / extend / accompany) | `generate_music`, `get_music`, `list_music` | `POST /v1/music`, `GET /v1/music/{id}`, `GET /v1/music` |
| Speech with preset speakers or a quick clone (no training) | `generate_speech`, `get_speech`, `list_speech` | `POST /v1/speech`, … |
| Pictures | `generate_image`, `get_image`, `list_images` | `POST /v1/images`, … |
| Video projects | `create_project`, `update_project`, `add_file`, `render_video`, `get_status`, … | `/v1/projects/…` |

`/v1` mirrors take the same arguments as the MCP tool's input schema (`GET /mcp` → `tools/list` gives the
authoritative JSON Schema for each) and return the same objects.

---

## 8. MCP tools

**[exists]** `https://studio.abhinava.xyz/mcp` — stateless Streamable HTTP, same OAuth (§3). Users add it to
Claude / ChatGPT with "install studio.abhinava.xyz/install". Current tools:

`list_projects, get_project, create_project, update_project, add_media, add_file, get_upload_link,
plan_scenes, set_scenes, render_video, get_status, cancel_render, generate_image, get_image,
list_images, generate_speech, get_speech, list_speech, generate_music, get_music, list_music`

**[new]** voice tools. Each is a thin wrapper over the `/v1` endpoint shown, with the same arguments and
results.

| Tool | Wraps | Notes for the tool description |
|---|---|---|
| `list_voices` | `GET /v1/voices` | read-only |
| `get_voice` | `GET /v1/voices/{id}` | read-only; includes profile and training progress |
| `get_voice_upload_link` | creates a one-time page where the person uploads clips **in their browser** | Assistants must never upload someone's voice on their behalf; returns a URL to give the person. |
| `train_voice` | `POST /v1/voices/{id}/train` | only after the person uploaded and attested |
| `resing_song` | `POST /v1/voices/{id}/resing` | "change the singer of a song to my voice" |
| `sing_song` | `POST /v1/voices/{id}/songs` | "write and sing a song in my voice" |
| `speak_as_voice` | `POST /v1/voices/{id}/speech` | |
| `get_voice_job` | `GET /v1/jobs/{id}` | poll every ~20 s |

Voice creation, consent and clip upload are deliberately **not** exposed as MCP tools. A person attests and
uploads in a browser page (`get_voice_upload_link`), not an assistant acting for them.

Calls that can finish within ~70 s wait in-call and return the finished result. Otherwise they return the
job with `status: queued|running` and the assistant polls (Cloudflare cuts requests at ~100 s).

---

## 9. Limits, errors, privacy

### 9.1 Limits (per account; admin can mark an account unlimited)

| Thing | Limit |
|---|---|
| Voices | 3 |
| Minimum to train a voice | **5 min** of usable singing (a warning below 10 min) |
| Clips per voice | 40, total ≤ 60 min, each 5 s – 10 min, ≤ 100 MB |
| Voice trainings | 3 per voice per day |
| Style model songs | 3–40 (≥ 8 min total), each 30 s – 6 min |
| Style trainings | 1 per voice per day |
| Re-sing / songs / convert | 12 per hour (shared with `generate_music`) |
| Voice speech | 30 per hour (shared with `generate_speech`) |
| Source length for re-sing | 6 min |
| Everything made or uploaded (clips, results, songs, speech, pictures, projects) | **90 days** after it last changed, then deleted — every object carries `deleted_on` / `days_left`; tell people to keep their own copies |
| Trained voice model + voice profile | **kept until the owner deletes the voice** (never expires) |

Over a limit → `429` with `Retry-After` (seconds).

### 9.2 Errors

All errors are JSON: `{"error": "<plain English the app may show to the user>", "code": "<machine code>"}`.

| HTTP | `code` | When |
|---|---|---|
| 400 | `invalid_request` | Bad or missing field; `error` says which and how to fix it. |
| 400 | `not_enough_audio` | `train` before minimums; `error` says how many more minutes. |
| 400 | `bad_audio` | Unreadable, silent, or too short. |
| 401 | `unauthorized` | Missing/expired token, or account disabled/expired. |
| 404 | `not_found` | No such object **or not yours**. |
| 409 | `wrong_state` | E.g. deleting a clip while `training`; `error` names the state. |
| 413 | `too_large` | File over 100 MB. |
| 422 | `rejected_clip` | Clip refused (two voices, no voice found…). |
| 429 | `rate_limited` | See §9.1; `Retry-After` set. |
| 503 | `unavailable` | The voice engine is down; retry later. Jobs are never lost — they wait. |

A job that fails has `status: "failed"` and `error` in the same plain style, for example "the GPU stayed busy
for 45 minutes; please try again later".

### 9.3 Consent and privacy (product rules — the backend enforces them)

- **Attestation:** creating a voice requires `consent.attest: true`. The app must show this statement and
  require a tick box (unticked by default):
  > "This is my own voice, or I have the explicit permission of the person whose voice it is. I won't use it
  > to impersonate anyone or to deceive. I can delete it at any time."
  The server stores the statement version, time, account and client IP with the voice.
- **Owner-only:** a voice and its style model are usable only by the account that created it — in the web app,
  `/v1`, and MCP. They are never listed or usable by other accounts. The admin can see and delete voices.
- **Retention:** clips and job results are deleted 90 days after they last changed; the trained model and its profile never expire. After the clips expire, the voice still works, but retraining needs new recordings.
- **Deletion:** `DELETE /v1/voices/{id}` removes clips, model files, the profile, the style model, training data
  and caches within 1 minute. Deleting the account deletes all of its voices.
- **No export:** model files (`.pth`, `.index`, adapters) are never downloadable through the API.
- **Disclosure:** the app should label voice outputs as AI-generated where it shares them.

---

## 10. Backend implementation notes

For the person building the server side. Everything here was checked against this box on 2026-10-25.

### 10.1 Topology

```
app / MCP client ──HTTPS──► Cloudflare tunnel ──► web (FastAPI, :8120)  ── DB: sqlite /data/studio.db
                                                   │  app/gpuqueue.py  (one FIFO for all studio GPU work)
               ┌───────────────────┬───────────────┼──────────────────┬───────────────────────┐
               ▼                   ▼               ▼                  ▼                       ▼
        rvc  [new] :8098     music :8001       tts :8097         wis-api :8090        (shared RTX 4090)
        Ultimate RVC 0.6     ACE-Step 1.5      Qwen3-TTS +        pictures/videos
        separate/convert/    songs, LoKr       Whisper (CPU)      (own queue; the
        train                train/load                           studio waits for it)
```

- **`rvc` service:** compose service in `~/story-studio`, container `story-studio-rvc`, studio network only, no
  host port. It **reuses the existing image `rvc-docker-ultimate-rvc` and its venv read-only**
  (`~/Documents/RVC-Docker/venv:/app/venv:ro`). The user's own `ultimate-rvc` container on :8550 must not be
  touched. The 8.6 GB venv and the 4 GB of base models are mounted, not duplicated. Two package directories write at runtime, so they get writable copies mounted over them: `static_sox` (a lock file) and `ultimate_rvc/rvc/train/models/mute` (a spectrogram cache).
- Mounts (verified working as uid 1000):
  - `~/story-studio/rvc-data/models:/models` (writable: `rvc/voice_models`, `rvc/training`,
    `audio_separator` — a 178 MB copy)
  - `~/Documents/RVC-Docker/models/rvc/{embedders,predictors,pretraineds}` → `/models/rvc/…` **read-only**
  - `rvc-data/logs:/app/logs` (the package writes `/app/logs/ultimate_rvc.log` unconditionally)
  - `rvc-data/{audio,temp,cache,datasets}`
- Env: `URVC_MODELS_DIR=/models URVC_AUDIO_DIR=/data/audio URVC_TEMP_DIR=/data/temp
  URVC_CONFIG_DIR=/cache/config HOME=/cache XDG_CACHE_HOME=/cache NUMBA_CACHE_DIR=/cache/numba
  MPLCONFIGDIR=/cache/mpl`; working dir `/cache`; entrypoint `/app/venv/bin/python -m uvicorn server:app`.
- **Run every GPU operation in a child process** and exit it, so VRAM is fully returned between jobs. The
  trainer itself ends with `os._exit(0)`, so it must be a subprocess anyway.

### 10.2 GPU queue rules (extend `app/gpuqueue.py`)

- Add kinds with `NEED_MB` (measure; starting values): `rvc_convert` 6000, `rvc_train` 10000,
  `style_train` 20000 (upstream says ~17 GB), `song` 14000 (the existing music value).
- A job starts only when WIS reports an empty queue, no studio video render is running, and free VRAM ≥ NEED.
  Already implemented for speech/music; reuse it.
- **Training is sliced:** each slice is one queue job of about 5 minutes; then the GPU is released and the next
  slice re-queues **behind** anything that arrived meanwhile. Never hold the GPU for a whole training run.
- An out-of-memory error → requeue (max 4), never fail someone else's job.
- Multi-stage generation jobs (`song` = ACE-Step then RVC) hold their place: stage 2 runs as the same queue
  job right after stage 1.

### 10.3 Voice training pipeline (Ultimate RVC 0.6 core API)

```python
from ultimate_rvc.core.train.prepare import populate_dataset, preprocess_dataset
from ultimate_rvc.core.train.extract import extract_features
from ultimate_rvc.core.train.train   import run_training
```

1. **Clean each clip** on upload (CPU or a short GPU job):
   - separate with `UVR-MDX-NET-Voc_FT` (vocals) when music is detected;
   - dereverb with `Reverb_HQ_By_FoxJoy`;
   - drop silence;
   - reject when two voices are found (speaker-embedding clustering).
2. `populate_dataset(voice_id, cleaned_files)`, then `preprocess_dataset(voice_id, path, sample_rate=40k,
   filter_audio=True, split_method=AUTOMATIC, chunk_len=3.0, overlap_len=0.3)`.
3. `extract_features(voice_id, f0_method=RMVPE, embedder_model=CONTENTVEC, include_mutes=2)` — GPU, a few
   minutes.
4. **Sliced training:**
   - Call `run_training(voice_id, num_epochs=<slice end>, save_interval=<slice size>, batch_size=8,
     vocoder=HIFI_GAN, pretrained_type=DEFAULT, detect_overtraining=True, overtraining_threshold=50)`
     repeatedly, each in a fresh process.
   - The trainer resumes from `G_2333333.pth`/`D_2333333.pth` ("Resumed from epoch N").
   - It saves a checkpoint only when `epoch % save_interval == 0`, so **slice size must equal
     `save_interval`**.
   - Size the first slice at 10 epochs, time it, then pick each slice to last about 5 minutes.
   - Targets: `standard` 300 epochs, `high` 500, with overtraining detection allowed to stop early.
5. On completion, copy `<voice>_best.pth` + `added_*.index` to `voice_models/<voice_id>/` and **delete the G/D
   checkpoints** (~0.5 GB per voice). Keep the preprocessed dataset only until the next retrain decision
   (disk is tight).
6. Compute the **profile** (§5.6) from the cleaned singing clips:
   - **Pitch:** RMVPE f0, voiced frames only. Range = 5/25/50/75/95th percentiles.
   - **Vibrato:** take notes held at least 400 ms with a stable pitch; find the 4–8 Hz peak of
     detrended f0 (rate) and its depth in cents (extent).
   - **Breathiness:** a harmonic-to-noise ratio proxy.
   - **Brightness:** spectral centroid, normalised.
   - **Dynamics:** 10th–90th percentile of RMS in dB.
   - **`style_caption`:** a fixed template filled from these numbers.
   - **`reference_clip_id`:** the cleanest 20–40 s singing excerpt.
7. Render `preview_url`: the same 10-s melody sung through the voice, for a quick check.
8. **Classify singing vs speech** per clip: share of voiced frames on stable pitch plateaus, together with the
   fraction of f0 inside musical semitone bins. Thresholds to be tuned on real clips.

### 10.4 Conversion pipeline

- **Re-sing:** `ultimate_rvc.core.generate.song_cover.run_pipeline(source=<local file>, model_name=voice_id,
  n_semitones=…, f0_method=RMVPE, index_rate=0.3, rms_mix_rate=1.0, protect_rate=0.33,
  embedder_model=CONTENTVEC, main_gain/inst_gain/backup_gain=…, room_size/wet/dry/damping from mix.reverb,
  output_format=MP3)`.
  - It separates the main vocal, backing vocals and instrumental (MDX Voc_FT + KARA_2), dereverbs,
    converts, and mixes.
  - Note: its `n_semitones` shifts the **music too**. Use `n_octaves` for octave moves, which shift only the
    vocal. For a non-octave `key` shift, shift the vocal only, or move the whole song, and say which one in
    the result.
- **Single vocal / speech:** `ultimate_rvc.core.generate.common.convert(audio_track, directory, model_name,
  n_octaves, n_semitones, f0_method=RMVPE, content_type=SPEECH|AUDIO)`.
- **Speech:** Qwen3-TTS (the `tts` service, a preset speaker of the voice's gender and range) → `convert`.
- **New song:** ACE-Step `custom` render. Caption = user style + `profile.style_caption`. The voice's
  reference clip goes in as `reference_audio`. Choose a key whose melody range fits `profile.range` →
  separate → `convert` the lead → remix. Follow the ACE-Step rules in §10.5.

### 10.5 ACE-Step rules learned the hard way (all verified)

These apply to every ACE-Step call, including the existing music features:

- For source-audio tasks (cover, repaint, extract, lego, complete) the language model is skipped. **Caption
  and lyrics reach the model only from the request.** Empty lyrics on a sung source make it mumble.
- `audio_cover_strength` is the fraction of steps that look at the source. Use 1.0 for everything except
  cover; cover defaults to 0.5.
- Repaint needs `chunk_mask_mode=explicit`, or the whole song is regenerated. To extend a song, use repaint
  with the window past the end.
- Extract, lego and complete run only on the **XL base** model (50 steps, guidance 7, shift 3). Turbo runs
  8 steps at shift 3. The engine **silently falls back to turbo** if base isn't loaded: compare the result's
  `dit_model` with the requested model and fail on a mismatch.
- `track_classes` must be repeated form keys, not a JSON string.
- A style adapter: `POST /v1/lora/load {"lora_path": …, "adapter_name": voice_id}`, then
  `POST /v1/lora/scale`.
  - Only one adapter is active per engine, so songs that use different style models must be serialised.
    The queue already does this.
  - Unload after use.
- Host RAM is tight: with offload, the music engine holds about 34 GB when all its models are loaded, and it
  exits itself after 10 min idle. LoKr training will need the same care.

### 10.6 Style model training (LoKr)

**Findings from reading ACE-Step @ ca1e85f (2026-10-25). These change the plan below:**

- **No resume.** LoKr training always starts at epoch 0, with a fresh optimizer. Slicing needs a patch
  that adds `resume_from` (load `lokr_weights.safetensors` + `training_state.pt`).
- **It runs inside the music server as a thread.** Nothing stops `/release_task` running during training,
  and that would move the decoder off the card mid-training. The studio queue must hold all music jobs
  (and WIS) for the whole run.
- **Training is never cleaned up.** The half-trained adapter stays on the live decoder, so the engine must
  be restarted after every run.
- **The idle watcher kills it.** `music/studio_entry.py` exits the engine after 10 idle minutes and doesn't
  count training as busy; it must be taught to.
- **Paths must sit under `/app`.** Mount a writable `./music-training:/app/training`.
- **Memory is unmeasured.** The trainer casts the whole XL decoder to fp32 (≈16–18 GB of weights alone),
  so it may not fit in 24 GB. Gradient checkpointing is always on; there is no fp8 option. A pilot
  measurement is in progress.
- **Turbo only.** Training and adapter loading work only on the primary model (xl-turbo); `/release_task`
  has no per-request adapter, so `/v1/lora/load` is engine-wide state.
- **Data handling.** Audio is truncated at 240 s, and lyrics are capped at 512 tokens.

Upstream's training API in the music container (`acestep/api/train_api_*`):

1. **Build the dataset:** the songs plus `{name}.lyrics.txt` holding the confirmed lyrics. Auto-label caption,
   BPM and key with `/v1/dataset/auto_label`, and keep the person's lyrics.
2. **Preprocess to tensors:** `/v1/dataset/preprocess_async`.
3. **Train:** `/v1/training/start_lokr` with `tensor_dir`, `train_epochs` (≈800 for 10–20 songs per upstream
   guidance; tune), `save_every_n_epochs`, `gradient_checkpointing=true` to stay well under 24 GB, and the
   default `lokr_linear_dim` 64 / alpha 128 / lr 0.03.
   - **Slice it like voice training**: stop after N epochs and resume from the last save. Confirm the
     resume behaviour before relying on it.
   - **Unconfirmed:** whether the training API can stop cleanly between epochs.
4. **Export** with `/v1/training/export` to `style_models/<id>/adapter.safetensors`, then delete the tensors
   and checkpoints.

### 10.7 Data model (sqlite, new tables)

```
voices(id, user_id, name, status, consent_version, consent_at, consent_ip,
       epochs_done, epochs_target, slices_done, profile_json, style_model_id,
       last_error, created_at, updated_at)
voice_clips(id, voice_id, name, kind, duration_s, usable_s, status, checks_json,
            rejected_reason, path, created_at)
style_models(id, voice_id, status, epochs_done, epochs_target, adapter_path,
             last_error, created_at, updated_at)
style_songs(id, style_model_id, title, duration_s, lyrics, lyrics_source, status,
            language, bpm, key, caption, path, created_at)
voice_jobs(id, user_id, voice_id, kind, status, params_json, result_json, message,
           progress, stage, attempts, error, callback_url, idempotency_key,
           created_at, updated_at)
```

`voice_jobs` joins `gpuqueue.TABLES`, so queue positions are shared with speech and music.

### 10.8 House rules for this codebase

- Put business rules in a module (`app/voices.py`) that both the `/v1` handlers and the MCP tools call, never
  in a handler. Otherwise one front door skips a rule.
- Test changes on a throwaway web container (`-p 127.0.0.1:8121:8120 --network story-studio_default`,
  then `docker network connect wis_default <name>`; `STUDIO_COOKIE_SECURE=0`, a scratch `/data` holding a
  `sqlite3 .backup` copy of the DB). Then run `docker compose up -d --build web`, but only when no
  project render is running, because the render worker runs inside the web process.
- Don't touch other services on the box. Containers are `restart: unless-stopped` and run as `user: 1000:1000`.

---

## 11. Acceptance tests

Run each against a voice trained from a **synthetic** singer (a voice generated by the song model, extracted
as a stem), never a real person without consent.

| # | Test | Pass when |
|---|---|---|
| 1 | Create a voice without `consent.attest` | `400 invalid_request` |
| 2 | Upload a 2-voice duet clip | `422 rejected_clip` |
| 3 | `train` with 5 min of singing | `400 not_enough_audio`, saying 5 more minutes are needed |
| 4 | Train with 12 min of singing | `ready`; training ran in ≥ 3 slices; a WIS picture job submitted mid-training finished without waiting for the training to end |
| 5 | Profile | `range.mid_hz` within 1 semitone of the f0 median measured independently on the dataset |
| 6 | Re-sing a male-sung 40 s song with `key:auto` into a female voice | Output length = source; the music stem correlates ≥ 0.95 with the original instrumental; the vocal's median f0 is within 2 semitones of the voice's `mid_hz`; speaker similarity to the voice > similarity to the original singer |
| 7 | `speech` | Whisper transcript of the output matches the text with WER < 10% |
| 8 | Another account calls `GET /v1/voices/{id}` | `404` |
| 9 | `DELETE` voice | All files gone from `rvc-data` within 1 min; jobs using it fail with "the voice was deleted" |
| 10 | Style model on 10 synthetic songs, then `songs` with `style_strength` 0 vs 0.8 | 0.8 is measurably closer in vibrato rate/extent and phrase timing to the training songs |
| 11 | Kill the web container mid-slice | On restart the job resumes from the last saved epoch; nothing is lost |
| 12 | Disk | Steady-state disk per trained voice ≤ 150 MB (model + index + profile + kept clips excluded) |

---

## 12. Open questions

1. ~~Clip retention~~ **Decided:** clips are deleted 90 days after upload, like everything else; the trained
   model and profile are kept until the voice is deleted.
2. **Shift on re-sing:** for a non-octave key shift, move only the vocal (it may clash with the music)
   or the whole song (the music changes key)? Proposed: an octave move only by default; a non-octave
   shift moves the whole song and is opt-in.
3. **LoKr slicing:** **answered: it can't resume** (§10.6). Choose between patching upstream for resume and
   an exclusive overnight window, once the pilot has measured GPU memory and time.
4. **Disk:** 844 GB free after the 2026-10-25 cleanup. A trained voice takes about 120 MB, and its cleaned
   clips about 75 MB until they expire.
