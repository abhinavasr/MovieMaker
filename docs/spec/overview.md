# Story Studio — complete API specification

**Version:** 2.0 · **Date:** 2026-10-25 · **Owner:** Abhinava · **Base URL:** `https://studio.abhinava.xyz`

Story Studio is a single-GPU media studio. Everything below is **live**, unless it is marked **[not built]**.

| Capability | What it makes | Engine | Ways in |
|---|---|---|---|
| **Video projects** | A film from pictures + a story: a *storyboard* of animated scenes, or a *single* ~15 s clip with generated sound | MiniMax H3 (`minimax-h3-i2v`, `minimax-h3-ref2v`) via WIS | MCP · web pages |
| **Pictures** | Text-to-image and image-to-image (edit / combine up to 4 pictures) | FLUX.2 klein 9B via WIS | MCP |
| **Speech** | Text read aloud by 9 preset speakers, or a quick clone of a short voice clip; Burmese by 4 Burmese speakers or a clone; Hindi by 4 native Hindi speakers, the 9 presets (accented) or a clone | Qwen3-TTS (+ Whisper on the CPU for transcripts); VoxCPM2 for Burmese and native Hindi | MCP · web pages |
| **Songs** | Full songs with vocals, plus cover / repaint / extract / add-a-layer / extend / accompany on an existing track | ACE-Step 1.5 XL | MCP · web pages |
| **Personal voices** | A person's own trained voice (layers 1–2) and singing style (layer 3), used to re-sing songs, sing new ones and speak | RVC v2 + ACE-Step LoKr | MCP · **REST `/v1`** · web pages |

**Which interface to build on**

- **MCP** (`/mcp`) is the complete public API today. Every capability above is a tool. Part B documents every tool,
  generated from the live server's own schemas.
- **REST `/v1`** exists today **only for voices** (Part C). Plain-REST mirrors of the other tools
  (`/v1/projects`, `/v1/images`, `/v1/speech`, `/v1/music`) are **[not built]**. When built, they will take the
  same arguments and return the same objects as the MCP tools.
- `/api/*` routes are the web pages' private, cookie-only API. **Don't build on them.**

An app can call MCP directly: it is JSON-RPC over HTTPS (§A.2).

---

## Part A — How it all works

### A.1 Authentication (all interfaces)

OAuth 2.1: the authorization-code flow with PKCE, **device sign-in (RFC 8628)** for apps that can't redirect,
and dynamic client registration. Accounts are issued by the studio admin; there is no
public sign-up.

| Endpoint | Purpose |
|---|---|
| `GET /.well-known/oauth-protected-resource` (and `/mcp` suffix) | Resource metadata (RFC 9728) |
| `GET /.well-known/oauth-authorization-server` | Server metadata (RFC 8414) |
| `POST /oauth/register` | Dynamic client registration (RFC 7591) |
| `GET /oauth/authorize` | Sign-in page (studio username + password) |
| `POST /oauth/token` | Code → access + refresh tokens; refresh; device-code polling |
| `POST /oauth/device_authorization` | Device sign-in: get a code for the person to approve (below) |
| `GET/POST /activate` | Device sign-in: the page where the person enters the code and taps Allow |

- **Header:** send `Authorization: Bearer <access_token>` to `/mcp` and to `/v1/*`.
- **Missing or expired token:** `/mcp` answers `401` with a `WWW-Authenticate: Bearer resource_metadata=…`
  header, which is how clients discover where to sign in. `/v1` answers JSON `{"error","code":"unauthorized"}`.
- **Revocation:** disabling, expiring or deleting an account revokes its tokens on the next call.
- **Mobile apps:** register a custom-scheme redirect (`myapp://oauth`) and use PKCE S256. Embed no secret.
- **Sign-in inside a mini app, TV or script: device sign-in (RFC 8628).** Use this when the app can't
  redirect or open a pop-up. It needs no redirect address, and the person's password never passes through the
  app.

  ```http
  POST /oauth/register              {"client_name":"My mini app",
                                     "grant_types":["urn:ietf:params:oauth:grant-type:device_code","refresh_token"]}
                                    → {"client_id":"mcp_…"}             (once, then store it)
  POST /oauth/device_authorization  client_id=mcp_…                      (form-encoded)
                                    → {"device_code":"ssd_…","user_code":"WWVJ-NNHS",
                                       "verification_uri":"https://studio.abhinava.xyz/activate",
                                       "verification_uri_complete":"https://studio.abhinava.xyz/activate?code=WWVJ-NNHS",
                                       "expires_in":900,"interval":5}
  ```

  1. **Show the code.** The app shows "Go to studio.abhinava.xyz/activate and enter **WWVJ-NNHS**". Where
     links open outside the app, make `verification_uri_complete` a button or a QR code.
  2. **Approve.** The person signs in there, in any browser, and taps **Allow**.
  3. **Poll for the token.** Every `interval` seconds, the app sends
     `POST /oauth/token grant_type=urn:ietf:params:oauth:grant-type:device_code&device_code=ssd_…&client_id=mcp_…`.

  | Answer | Meaning | What to do |
  |---|---|---|
  | `authorization_pending` | Not approved yet | Keep polling |
  | `slow_down` | Polled too fast | Add 5 s to the interval |
  | `access_denied` | The person tapped **Cancel** | Stop |
  | `expired_token` | 15 minutes passed | Start again |
  | Tokens (`access_token`, `refresh_token`) | Approved | Use them |

  After that it is the normal token life: a 1 h access token, refreshed with `grant_type=refresh_token`.
- **Browser and mini apps (CORS):** `/mcp`, `/v1/*`, `/oauth/*`, `/.well-known/*`, `/dev` and the signed media
  links answer cross-origin calls from **any origin**, sandboxed `Origin: null` included. `OPTIONS` preflights
  get `204` with `Access-Control-Allow-Origin: *`, the requested headers allowed, and `WWW-Authenticate` exposed.
  Credentials (cookies) are **never** allowed cross-origin: send `Authorization: Bearer …` with
  `credentials: "omit"`, the browser default. Register as a public client (`token_endpoint_auth_method: "none"`)
  and use PKCE. The pages' cookie API `/api/*` has no CORS.

### A.2 Calling MCP from an app

`POST /mcp` takes JSON-RPC 2.0 over stateless Streamable HTTP (one request, one response, no session).

```http
POST /mcp
Authorization: Bearer <token>
Content-Type: application/json
Accept: application/json, text/event-stream

{"jsonrpc":"2.0","id":1,"method":"tools/call",
 "params":{"name":"generate_image","arguments":{"prompt":"a lighthouse at dusk","orientation":"landscape"}}}
```

- **Result:** `result.content[0].text` holds the tool's JSON result, as a string. `generate_image` adds an
  `image` content item, a small JPEG preview.
- **Errors:** a tool error comes back as `result.isError: true`, with a plain-English message in `content[0].text`.
- **Discovery:** `initialize` → `tools/list` returns the authoritative JSON Schema of every tool (Part B is
  generated from it). The server's `instructions` string, in the `initialize` result, is the usage guide that
  assistants read.
- **Assistants:** Claude / ChatGPT users just say "install studio.abhinava.xyz/install".

### A.3 Shared rules

- **One GPU.** Pictures and videos run through WIS's queue. Speech, songs and voice work run through the studio's
  own queue, which starts a job only when WIS is idle and enough VRAM is free. So any generation can wait.
  Every result reports where it is (`status`, `queue_position`, `message`).
- **Waiting within a call.** Calls that usually finish in time wait up to about 70 s and return the finished
  result. These are pictures, speech, songs and voice jobs. Otherwise they return the object still `queued` or
  `generating`, and you poll its `get_*` tool every ~10–20 s. Cloudflare cuts any request at ~100 s.
  Video renders never wait: poll `get_status` every 20–30 s.
- **Signed links.** Results carry download links that work without a token, so they can go straight into
  `<audio>`, `<img>` or `<video>`. They expire, and a fresh `get_*` call returns a fresh link.

| Link | Lifetime |
|---|---|
| Pictures, speech, songs, videos | **24 h** |
| Voice results | 7 days |
| Upload links | 1 h |

- **Retention.** Everything made or uploaded is **deleted 90 days after it last changed**. Every object carries
  `deleted_on` (a UTC date) and `days_left`. Apps should tell people to download what they want to keep.
  **Exception:** trained voices, voice profiles and singing-style models are kept until the owner deletes them.
- **Ownership.** Every object belongs to the account that made it. Another account gets "not found", never
  "forbidden".
- **Hourly limits** per account (the admin can mark an account unlimited):

| Kind | Limit per hour |
|---|---|
| Video renders | 10 |
| Pictures | 20 |
| Speech | 30, shared with voice speech |
| Songs | 12, shared with voice re-sing and voice songs |

  Separately, the studio's whole WIS key allows 60 picture/video jobs an hour across all users.
- **Uploads into projects:** up to 16 MB per file.

### A.4 Video projects

A **project** holds a story, pictures and optional audio. A **render** turns it into a video. There are two
modes, and `update_project(mode=…)` switches between them:

| | `storyboard` (with scenes) | `single` (without scenes) |
|---|---|---|
| Model | `minimax-h3-i2v`: animates one picture per scene | `minimax-h3-ref2v`: one clip from the story with reference pictures |
| Length | One clip per scene, 5 s by default (`seconds`), up to **12** scenes, joined into one film | One clip, up to ~15 s |
| Pictures | One per scene, 12 slots | Up to **9** references, named `<Picture 1>`, `<Picture 2>`… in the story |
| Audio | **Silent**, unless you add a soundtrack (1 audio slot: a song, speech, or an upload) | **Generated stereo sound**; up to 3 reference audio clips |
| Prompts | Each scene's prompt should describe **what moves and how the camera moves**; the model already has the picture | The story is the prompt |
| Match cuts | `match_cuts: true` uses scene *N*'s last frame as the start of scene *N+1*, so clips join without a jump | n/a |

**Sizes** (`orientation`): landscape 864×480, portrait 480×864, square 640×640.

**Upscale 2× (experimental, added 2026-11-21, off by default):** `update_project(upscale=…)` or the
project page. The video is made at the size above and delivered at twice it, with its sound, by an H3
latent upscaler in WIS. `latent` takes about 2.4× the render time and is sharper, but can invent fine
texture. `refine` takes about 2.7× and adds a short second sampling pass, giving the cleanest detail.
Measured on one 5 s portrait clip: 105 s plain, 250 s latent, 280 s refine.

**Typical flow, storyboard:**
1. `create_project(title, mode:"storyboard", story)`.
2. Add pictures with `add_file(image_id=…)` for pictures made with `generate_image`. Alternatives: `add_file(file=…)`
   for a file in the chat, `add_media(urls=[…])` for public URLs, or `get_upload_link` for a curl or browser upload.
3. `plan_scenes`. The studio's planner splits the story into one prompt per scene. When the planner is busy with
   a render it falls back to an even sentence split and says so, so you can rewrite with `set_scenes`.
4. Optionally `set_scenes([{scene, prompt, title?, seconds?}])`.
5. `render_video`. It returns at once, then you poll `get_status` until `succeeded` and use `video_url`.
6. **Re-render part of it:** `render_video(scenes:[2,4])` regenerates only those scenes and reuses the others.
   Measured: 100 s for one of two scenes, against 190 s for both.

**Single clip:** `create_project(mode:"single", story:"<Picture 1> walks into <Picture 2>…")`, add up to 9
pictures (and up to 3 audio clips), `render_video`, then poll.

**Timing:** about 2 minutes per 5 seconds of video. One project renders at a time; `renders_waiting` shows the
queue. `cancel_render` also stops the job in WIS.

**`get_status` result (real):**
```json
{"project_id":"prj_b139…","status":"succeeded","progress":100,"message":"done","renders_waiting":0,
 "scenes":[{"scene":1,"status":"done"},{"scene":2,"status":"done"}],
 "video_url":"https://studio.abhinava.xyz/v/prj_b139…/<exp>/<sig>/video.mp4","video_has_sound":false}
```

`get_project` returns the full project: settings, pictures, audio, scenes with prompts, status, `page_url`,
and `deleted_on` / `days_left`.

### A.5 Pictures

- **`generate_image(prompt)`** draws from the prompt alone. Pass `orientation` (square 1024², portrait
  864×1536, landscape 1536×864) or an explicit `width`/`height`.
- **`model`** (added 2026-11-22): `flux-2-klein-9b` (default), `qwen-image-2.1-turbo` (~10 s) or
  `qwen-image-2.1` (base, 25 steps, ~15-46 s). Qwen-Image-2.1 is one 7B model that draws and edits and is
  notably good at lettering. Its weights are under the Qwen research licence; check it before any
  commercial use.
- **Image-to-image:** add up to **4 source pictures** to edit or combine them, saying in the prompt what to
  change. Sources are numbered in this order, and that order is what "picture 1" means in a prompt:
  1. `file` (attached in the chat)
  2. `base64`
  3. `image_ids` (earlier results)
  4. `pictures` (a project's pictures, with `project_id`)
  5. `urls`
- **Framing:** an edit with no size given is drawn at the first source's size, which keeps the framing.
- **Speed:** usually 8–30 s, inside the call. If it's still generating, poll `get_image`.
- **Editing a result:** pass its `image_id` as a source.
- **Using one in a video:** `add_file(image_id=…)`.
- **History:** `list_images` returns the 30 most recent pictures.

**Result (real):**
```json
{"image_id":"img_8025…","status":"succeeded","prompt":"…","made_from":"1 source picture(s)",
 "size":"1536x864","seed":3317594126108643,"deleted_on":"2027-01-22","days_left":89,
 "image_url":"https://studio.abhinava.xyz/i/img_8025…/<exp>/<sig>/image.png","link_expires_in_hours":24}
```

### A.6 Speech

- **`generate_speech(text)`** reads text with one of **9 preset speakers**, steered by `instruct` (e.g.
  "calm and slow"). The speakers:

| Speaker | Voice |
|---|---|
| Vivian | female, Chinese-native, bright young voice |
| Serena | female, Chinese-native, warm and gentle |
| Ono_Anna | female, Japanese-native, light and playful |
| Sohee | female, Korean-native, warm and emotional |
| Ryan | male, English-native, dynamic and energetic (default) |
| Aiden | male, American English, sunny and clear |
| Uncle_Fu | male, Chinese-native, low and mellow, older |
| Dylan | male, Beijing Chinese, youthful and natural |
| Eric | male, Sichuan Chinese, lively and slightly husky |

- **Quick clone, no training:** give a reference clip (`file`, `reference_base64` or `reference_url`) of
  8–20 s of clean speech, ≤ 30 s.
  - `match:"style"` (the default) copies accent, rhythm and delivery. It needs the clip's words: give
    `reference_text`, or they are transcribed automatically by Whisper on the CPU.
  - `match:"timbre"` copies only the colour of the voice.
  - `temperature` sets expressiveness (clones default to 0.7). `seed` repeats a result. `takes` makes 1–3
    variants.
- **Burmese (Myanmar), added 2026-11-20:** spoken by **VoxCPM2** (OpenBMB, Apache-2.0) in its own container.
  - Text that is mostly Burmese script, or `language:"Burmese"`, goes there. Auto detects it.
  - It has its own speakers: **Thiri** (default; female, warm and gentle, calm storyteller), **Nandar**
    (female, bright and cheerful), **Kyaw** (male, clear and confident) and **U_Ba** (male, deep and slow,
    older). They speak only Burmese, and the nine speakers above cannot speak Burmese; a mismatch is a 400.
  - `instruct` works. A clone can speak Burmese whatever language its clip is in. Burmese clones use the
    audio alone, so `reference_text`, `match` and `temperature` do not apply.
  - Text is spoken sentence by sentence and joined. Digits are read as numbers.
  - Chosen by a measured comparison of five open models. With a Burmese ASR as judge it scored about 5%
    syllable errors on news sentences; real human recordings scored 5.0%.
- **Hindi, added 2026-11-21:** two choices of voice.
  - Text that is mostly Devanagari, or `language:"Hindi"`, is Hindi. Auto detects it.
  - **Native Hindi voices on VoxCPM2** (the default): **Meera** (default; female, warm and gentle, calm
    storyteller), **Anaya** (female, bright and cheerful), **Arjun** (male, clear and confident) and
    **Vikram** (male, deep and slow, older). They speak only Hindi; a mismatch is a 400.
  - **The nine Qwen speakers** can also be chosen for Hindi. They are clear but carry a foreign accent.
  - Measured with Whisper as judge: on two test sentences the native voices made 0-6% character errors, the Qwen
    voices 2-10%. The gap is mostly accent (श said as स); Whisper writes Vikram's Hindi in Urdu script.
  - `instruct` works with both. Hindi clones run on VoxCPM2 and use the audio alone, as Burmese ones do.
- **Sanskrit śloka chant, added 2026-11-21:** `language:"Sanskrit"` (or `speaker:"Vagdhenu"`) CHANTS the
  text with **Vāgdhenu** (IISc, Apache-2.0), a metre-aware śloka-to-chant model with one voice. It is
  never auto-detected, because Hindi and Sanskrit share Devanagari.
  - Any Indian script or IAST. Several ślokas (each ending in ॥) are chanted one at a time and joined
    with a short pause.
  - `meter` picks the chandas (anuṣṭubh, vasantatilakā, mālinī, …) or `auto` (default) detects it per
    śloka. The clip's message names the metre used and says when detection fell back.
  - No cloning, no `instruct`, and not available in `speak_as_voice`.
- **Speed:** usually inside the call. If not, poll `get_speech`. `list_speech` lists recent clips.
- **As a soundtrack:** `add_file(speech_id=…)` puts a clip into a project.

**Result (real):**
```json
{"speech_id":"spk_23f4…","status":"succeeded","text":"…","voice":"cloned from a reference clip",
 "language":"Auto","match":"style","temperature":0.95,"seed":392707305,
 "reference_transcript_used":"…","duration_s":9.9,"deleted_on":"2027-01-23","days_left":89,
 "audio_url":"https://studio.abhinava.xyz/s/spk_23f4…/<exp>/<sig>/speech.mp3","link_expires_in_hours":24}
```

For a person's own **trained** voice, use Part C (`speak_as_voice`), not a quick clone.

### A.7 Songs

- **`generate_music`** makes songs with real vocals (ACE-Step 1.5 XL). `task` picks what it does:

| `task` | Give | What it does | Model |
|---|---|---|---|
| `describe` | `description` ("an upbeat Hindi wedding song about the monsoon") | Writes the lyrics, style and structure, and sings it | XL turbo + 4B composer |
| `custom` (default) | `lyrics` with `[Verse]`/`[Chorus]` tags + `style`, or `instrumental:true` | Your words in your style; a `reference_*` track can steer the sound | XL turbo |
| `cover` | a `source_*` + `style` (**required**) [+ new `lyrics`] | Re-performs the song in a new style; `strength` 0–1 (default 0.5; 0.3 = big genre change, 0.8 = subtle) | XL turbo |
| `repaint` | a `source_*` + `start`/`end` (3–90 s) | Regenerates only that stretch; the rest stays identical | XL turbo |
| `extend` | a `source_*` + `add_seconds` (10–120, default 30) | Continues past the end | XL turbo |
| `extract` | a `source_*` + `track` (vocals, drums, bass…) | Pulls out one stem | XL base |
| `add_layer` | a `source_*` + `track` + `style` describing the new part (**required**) | Generates a new stem that fits | XL base |
| `accompany` | a `source_*` + `instruments` + `style` (**required**) | Builds a band around a single part (e.g. an a-cappella vocal) | XL base |

- **Sources:** give exactly one of `source_music_id`, `source_speech_id`, `source_file`, `source_base64` or
  `source_url`; the same forms work for `reference_*`.
- **Lyrics for source tasks:** for cover, repaint and extend, if you give no lyrics the source's own are used.
  For a song made here they are reused; for an upload they are transcribed. Empty lyrics make the model mumble.
- **Options:** `language` (code, or `unknown` = auto), `duration` (10–360 s), `bpm`, `key`, `time_signature`,
  `seed`, `title`.
- **Speed:** a 40 s song takes about 60 s once it starts; stem tasks take 15–30 s. It waits in-call up to ~70 s,
  then poll `get_music`. `list_music` lists recent songs.
- **In a video:** `add_file(music_id=…)` uses a song as a storyboard soundtrack.

**Result (real):**
```json
{"music_id":"mus_6bc2…","status":"succeeded","task":"extract","title":"Pull out one stem",
 "requested_track":"vocals","duration_s":45.7,"seed":"2658947028","deleted_on":"2027-01-23","days_left":89,
 "audio_url":"https://studio.abhinava.xyz/m/mus_6bc2…/<exp>/<sig>/song.mp3","link_expires_in_hours":24}
```

### A.8 Personal voices (summary; full detail in Part C)

A person trains **their own** voice from their recordings in the browser (consent required, owner-only). Then
assistants and apps can use it:

- `resing_song`: change a song's singer to them, mixed back onto the original music.
- `sing_song`: a new song in their range and manner, optionally performed with their trained singing style.
- `speak_as_voice`: speech in their voice.

Creating a voice and uploading recordings are browser-only on purpose; `get_voice_upload_link` gives the page.

### A.9 Errors (MCP)

Tool errors come back as `isError: true` with a plain-English message that says what to fix. Examples:

- "describe the new sound for the cover, e.g. …"
- "repaint a stretch of 3–90 seconds; for longer changes use a cover"
- "you have made 12 songs in the last hour, which is the limit for this account"

Show the message to the person as it is. A job that fails later has `status:"failed"` and an `error` in the
same style.

### A.10 Not built yet

- **REST mirrors** for projects, pictures, speech and songs (`/v1/projects`, `/v1/images`, `/v1/speech`,
  `/v1/music`).
- **Voice extras:** webhooks, pagination, `Idempotency-Key`, `/v1/uploads`, `/v1/voices/{id}/convert`,
  adding a voice result straight into a project, `preview_url`, and rejecting recordings that contain two
  voices. See Part C for the voice parts.
