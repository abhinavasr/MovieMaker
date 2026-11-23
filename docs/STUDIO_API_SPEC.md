# Story Studio — complete API specification

**Version:** 2.0 · **Date:** 2026-10-25 · **Owner:** Abhinava · **Base URL:** `https://studio.abhinava.xyz`

Story Studio is a single-GPU media studio. Everything below is **live**, unless it is marked **[not built]**.

| Capability | What it makes | Engine | Ways in |
|---|---|---|---|
| **Video projects** | A film from pictures + a story: a *storyboard* of animated scenes, or a *single* ~15 s clip with generated sound | MiniMax H3 (`minimax-h3-i2v`, `minimax-h3-ref2v`) via WIS | MCP · web pages |
| **Pictures** | Text-to-image and image-to-image (edit / combine up to 4 pictures) | FLUX.2 klein 9B via WIS | MCP |
| **Speech** | Text read aloud by 9 preset speakers, or a quick clone of a short voice clip; Burmese by 4 Burmese speakers or a clone | Qwen3-TTS (+ Whisper on the CPU for transcripts); VoxCPM2 for Burmese | MCP · web pages |
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

---

## Part B — MCP tool reference (generated from the live server)

29 tools. Each one is `tools/call` with `{"name": …, "arguments": {…}}`. Arguments not listed are rejected (`additionalProperties: false`).

### Video projects

#### `list_projects` — List projects *(read-only)*

List the signed-in person's video projects, newest first, with each one's mode (storyboard = with scenes, single = without) and whether it has a finished video.

*No arguments.*

#### `get_project` — Get a project *(read-only)*

Everything about one project: story, settings, pictures, each scene's prompt, render status, and a download link for the finished video. Read this before regenerating.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `project_id` | string | **yes** | A project id, e.g. prj_1a2b… |

#### `create_project` — Create a project

Start a new video project. It has no pictures yet; add them next.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `title` | string | **yes** |  |
| `mode` | string — one of: `storyboard`, `single` | **yes** | storyboard = WITH scenes (one picture per scene, joined). single = WITHOUT scenes (one clip, pictures are references, has sound). |
| `story` | string |  | Storyboard: the story to split into scenes. Single: the prompt itself — motion, camera and sound. |

#### `update_project` — Change story or settings

Change a project's title, story or settings; anything left out is kept. Setting `mode` switches the same pictures and story between WITH scenes (storyboard) and WITHOUT scenes (single) — then call render_video to regenerate it that way. Nothing is rendered by this call.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `project_id` | string | **yes** | A project id, e.g. prj_1a2b… |
| `title` | string |  |  |
| `story` | string |  | Storyboard: raw material for plan_scenes. Single: the prompt sent to the model; refer to pictures as <Picture 1>, <Picture 2>… |
| `mode` | string — one of: `storyboard`, `single` |  | storyboard = WITH scenes (one picture per scene, joined). single = WITHOUT scenes (one clip, pictures are references, has sound). |
| `orientation` | string — one of: `portrait`, `landscape`, `square` |  | portrait 480x864, landscape 864x480, square 640x640 |
| `seconds` | integer (≥ 1, ≤ 15) |  | Single: clip length. Storyboard: length given to newly planned scenes (existing scenes keep theirs). |
| `match_cuts` | boolean |  | Storyboard only: each scene ends on the next scene's picture, so clips join without a jump. |
| `reference_fidelity` | string — one of: `match`, `max` |  | Single only: max = stronger likeness, much slower. |

#### `add_media` — Add pictures or audio by URL

Add pictures (PNG/JPEG/WebP) or audio to a project from public http(s) URLs, in order. In a storyboard each picture becomes a scene; in a single clip each is a numbered reference. For a file in this chat use add_file instead.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `project_id` | string | **yes** | A project id, e.g. prj_1a2b… |
| `urls` | array of string | **yes** |  |

#### `add_file` — Add an attached or generated file

Add ONE picture (PNG/JPEG/WebP) or audio file that is in this conversation — an image you generated or a file the person attached — to a project. Pass it as `file`, or pass `image_id` for a picture made with generate_image, or `speech_id` for a clip made with generate_speech, or `music_id` for a song from generate_music. Call once per file, in the order the pictures should be numbered. Use this instead of get_upload_link whenever the file lives in the chat.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `project_id` | string | **yes** | A project id, e.g. prj_1a2b… |
| `image_id` | string |  | A finished picture from generate_image, e.g. img_1a2b… |
| `speech_id` | string |  | A finished clip from generate_speech, e.g. spk_1a2b… — added as audio (a storyboard soundtrack). |
| `music_id` | string |  | A finished song from generate_music, e.g. mus_1a2b… — added as audio (a storyboard soundtrack). |
| `file` | object {download_url, file_id} |  | The conversation file to add. |
| `base64` | string |  | Alternative to `file`: the file's bytes, base64 encoded. Only practical for small files. |
| `filename` | string |  | Optional name; its extension decides picture vs audio. |

#### `get_upload_link` — Get an upload link *(read-only)*

A one-hour link that accepts picture and audio files for one project by HTTP POST, with a ready curl command — for files on a disk you can reach with a shell. For a file in this conversation (generated or attached), use add_file instead.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `project_id` | string | **yes** | A project id, e.g. prj_1a2b… |

#### `plan_scenes` — Plan scenes from the story *(destructive)*

Storyboard only. Split the project's story into one scene prompt per picture, REPLACING the current scenes. Skip this if you would rather write the prompts yourself with set_scenes.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `project_id` | string | **yes** | A project id, e.g. prj_1a2b… |

#### `set_scenes` — Write scene prompts

Storyboard only. Set the prompt, title or length of scenes by number (scene 1 is the first picture). Only the scenes listed change. A prompt should say what moves and how the camera moves — not describe the still picture.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `project_id` | string | **yes** | A project id, e.g. prj_1a2b… |
| `scenes` | array of object | **yes** |  |

#### `render_video` — Generate or regenerate the video

Queue a render of the project as it now stands, replacing its previous video. Storyboard: renders every scene and joins them, or pass `scenes` to regenerate only those and keep the other clips. Single: renders the one clip. Returns immediately — poll get_status. Uses real GPU time and counts against the account's hourly render limit.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `project_id` | string | **yes** | A project id, e.g. prj_1a2b… |
| `scenes` | array of integer |  | Storyboard only: scene numbers to regenerate. Omit to regenerate the whole video. |

#### `get_status` — Check a render *(read-only)*

Render progress for a project, per scene, and the video download link once it has succeeded.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `project_id` | string | **yes** | A project id, e.g. prj_1a2b… |

#### `cancel_render` — Cancel a render

Stop a queued or running render and free the GPU. The previous video, if any, is kept.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `project_id` | string | **yes** | A project id, e.g. prj_1a2b… |

### Pictures

#### `generate_image` — Generate a picture

Make a still picture on the studio's GPU. With only a prompt it is text-to-image. With source pictures it is image-to-image: the prompt says what to change, or how to combine them ("the person in picture 1 standing in the room from picture 2"). Up to 4 sources, from any mix of `file`, `base64`, `image_ids`, project `pictures` and `urls` — numbered in THAT order, which is what "picture 1" in the prompt refers to. An edit with no size given comes back at the first source's size. Usually returns the finished picture within the call; if `status` is not `succeeded`, call get_image. Counts against the account's hourly image limit.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `prompt` | string | **yes** | What to draw, or for image-to-image, what to change. |
| `orientation` | string — one of: `square`, `portrait`, `landscape` |  | square 1024x1024, portrait 864x1536, landscape 1536x864. For a storyboard scene, match the project's orientation. |
| `width` | integer (≥ 256, ≤ 2048) |  | Exact size instead of orientation; give height too. |
| `height` | integer (≥ 256, ≤ 2048) |  |  |
| `seed` | integer (≥ 0) |  | Repeat a result exactly with the same prompt and seed. |
| `file` | object {download_url, file_id} |  | Image-to-image: a picture in this conversation. |
| `base64` | string |  | Image-to-image: a picture's bytes, base64 encoded. |
| `image_ids` | array of string |  | Image-to-image: earlier results from generate_image — pass the last one to refine it. |
| `urls` | array of string |  | Image-to-image: public http(s) links to pictures. |
| `project_id` | string |  | Only with `pictures`: the project they are in. |
| `pictures` | array of integer |  | Image-to-image: picture numbers in project_id. |

#### `get_image` — Check a picture *(read-only)*

Status of a picture from generate_image; once it has succeeded, the picture itself and a download link.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `image_id` | string | **yes** | e.g. img_1a2b… |

#### `list_images` — List pictures *(read-only)*

The signed-in person's 30 most recent pictures from generate_image, newest first, with prompts and download links.

*No arguments.*

### Speech

#### `generate_speech` — Speak text aloud

Turn text into speech with Qwen3-TTS on the studio's GPU. By default a preset `speaker` reads it (Ryan if none given), optionally steered with `instruct`. To speak in someone else's voice instead, pass ONE reference clip (`file`, `reference_base64` or `reference_url`, 5-30 s of clear speech). BURMESE: text in Burmese script (or language=Burmese) is spoken by a separate engine, VoxCPM2, with its own speakers Thiri (default), Nandar, Kyaw and U_Ba; the other speakers cannot speak Burmese and these four speak only Burmese. A clone can speak Burmese whatever language the clip is in; for Burmese the clone uses the audio only, so reference_text, match and temperature have no effect. By default (match=style) the clone copies the speaker's accent, pace and delivery: that needs the clip's exact words, which are transcribed automatically if `reference_text` is not given (pass it when you know it — accuracy matters). match=timbre copies only the voice colour. `takes` (1-3) makes variations to choose from; `seed` repeats one. Up to 1200 characters per clip — split longer text. Usually returns the finished clip within the call; if `status` is not `succeeded`, call get_speech. While the GPU is busy with pictures or videos the clip waits in a queue (the reply says its position) and runs when the card is free. Counts against the account's hourly speech limit.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `text` | string (max length 1200) | **yes** | The words to speak. |
| `speaker` | string — one of: `Vivian`, `Serena`, `Ono_Anna`, `Sohee`, `Ryan`, `Aiden`, `Uncle_Fu`, `Dylan`, `Eric`, `Thiri`, `Nandar`, `Kyaw`, `U_Ba` |  | Preset voice. Vivian (female, Chinese-native, bright young voice); Serena (female, Chinese-native, warm and gentle); Ono_Anna (female, Japanese-native, light and playful); Sohee (female, Korean-native, warm and emotional); Ryan (male, English-native, dynamic and energetic); Aiden (male, American English, sunny and clear); Uncle_Fu (male, Chinese-native, low and mellow, older); Dylan (male, Beijing Chinese, youthful and natural); Eric (male, Sichuan Chinese, lively and slightly husky); Thiri (female, Burmese-native, warm and gentle, calm storyteller); Nandar (female, Burmese-native, bright and cheerful, lively); Kyaw (male, Burmese-native, clear and confident, friendly); U_Ba (male, Burmese-native, deep and slow, older and wise). The first nine speak every language except Burmese; Thiri, Nandar, Kyaw and U_Ba speak only Burmese. |
| `language` | string — one of: `Auto`, `English`, `Chinese`, `Japanese`, `Korean`, `German`, `French`, `Russian`, `Portuguese`, `Spanish`, `Italian`, `Burmese` |  | Language of the text; Auto detects it (Burmese script is detected and routed to the Burmese voices). |
| `instruct` | string (max length 500) |  | Preset speakers only: how to say it, e.g. "warm and slow, like a bedtime story" or "angry, shouting". |
| `file` | object {download_url, file_id} |  | Voice clone: an audio clip in this conversation. |
| `reference_base64` | string |  | Voice clone: an audio clip's bytes, base64. |
| `reference_url` | string |  | Voice clone: a public http(s) link to a clip. |
| `reference_text` | string |  | Voice clone: the exact words spoken in the reference clip. Transcribed automatically when omitted. |
| `match` | string — one of: `style`, `timbre` |  | Voice clone: style (default) = accent, pace and delivery too; timbre = voice colour only. |
| `temperature` | number (≥ 0.1, ≤ 1.5) |  | Expressiveness / variation. Clones default to 0.7; lower is flatter, higher is livelier. |
| `seed` | integer (≥ 0) |  | Repeat a take exactly (same text, voice and settings). |
| `takes` | integer (≥ 1, ≤ 3) |  | How many variations to make (each counts toward the hourly limit). |

#### `get_speech` — Check a speech clip *(read-only)*

Status of a clip from generate_speech, and its audio link once it has succeeded.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `speech_id` | string | **yes** | e.g. spk_1a2b… |

#### `list_speech` — List speech clips *(read-only)*

The signed-in person's 30 most recent clips from generate_speech, newest first, with their text and audio links.

*No arguments.*

### Songs

#### `generate_music` — Make a song

Make music with ACE-Step 1.5 on the studio's GPU. `task` picks what to do:
- describe: one sentence in `description`; the model writes lyrics, style and structure itself.
- custom (default): your `lyrics` — use [Verse], [Chorus], [Bridge], [Intro], [Outro] tags on their own lines — plus a `style` (genre, instruments, mood, vocal type, tempo: "female vocal, acoustic folk, fingerpicked guitar, warm, 90 bpm"). Set instrumental=true for no vocals. A reference_* track steers the sound.
- cover: re-perform source_* in a new `style` (required) — the original words are kept unless you give new `lyrics`; `strength` 0-1 = how closely to follow the source (default 0.5; 0.3 = big genre change, 0.8 = subtle).
- repaint: regenerate seconds `start`..`end` (3-90 s) of source_*; the rest is kept. `style`/`lyrics` default to the source song's.
- extract: pull one stem (`track`) out of source_*.
- add_layer: generate a new stem (`track`) that fits source_*; `style` describes only that new part (required).
- extend: make source_* longer by `add_seconds` (10-120, default 30); optional `lyrics` are sung in the new part.
- accompany: build `instruments` around a single track (e.g. a band under an a-cappella vocal); `style` describes the arrangement (required).
For an uploaded source with no `lyrics`, the words are transcribed from it.
Usually 20-90 s once it starts. While the GPU is busy with pictures or videos it waits in a queue; the reply gives queue_position — then poll get_music every ~20 s. Give the person the audio_url. Counts against the account's hourly music limit.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `task` | string — one of: `describe`, `custom`, `cover`, `repaint`, `extract`, `add_layer`, `extend`, `accompany` |  |  |
| `description` | string (max length 1000) |  | describe: what the song should be, e.g. "an upbeat Hindi wedding song about the monsoon". |
| `lyrics` | string (max length 4000) |  | The words to sing, with [Verse]/[Chorus] section tags. |
| `style` | string (max length 1000) |  | Genre, instruments, mood, vocal type, tempo. |
| `instrumental` | boolean |  | No vocals. |
| `language` | string — one of: `en`, `hi`, `zh`, `ja`, `ko`, `es`, `fr`, `de`, `pt`, `it`, `ru`, `ar`, `bn`, `ta`, `te`, `mr`, `pa`, `ur`, `sa`, `unknown` |  | Language the lyrics are sung in (code). unknown = auto. |
| `duration` | number (≥ 10, ≤ 360) |  | Length in seconds; omit to let the model choose. |
| `bpm` | integer (≥ 30, ≤ 300) |  |  |
| `key` | string |  | e.g. "C major", "A minor". |
| `time_signature` | string — one of: `2`, `3`, `4`, `6` |  | Beats per bar: 2, 3, 4, or 6 (6/8). |
| `seed` | integer (≥ 0) |  | Repeat a result with the same inputs and seed. |
| `strength` | number (≥ 0, ≤ 1) |  | cover: how closely to follow the source (default 0.5). |
| `start` | number (≥ 0) |  | repaint: from (s). |
| `end` | number (≥ 0) |  | repaint: to (s). |
| `add_seconds` | number (≥ 10, ≤ 120) |  | extend: how many seconds to add (default 30). |
| `track` | string — one of: `vocals`, `backing_vocals`, `drums`, `bass`, `guitar`, `keyboard`, `strings`, `synth`, `percussion`, `brass`, `woodwinds`, `fx` |  | extract / add_layer: which stem. |
| `instruments` | array of string (vocals, backing_vocals, drums, bass, guitar, keyboard, strings, synth, percussion, brass, woodwinds, fx) |  | accompany: instruments to build around the track. |
| `title` | string |  | Optional name for the song. |
| `source_music_id` | string |  | source track for cover/repaint/extract/add_layer/extend/accompany: an earlier song from generate_music. |
| `source_speech_id` | string |  | source track for cover/repaint/extract/add_layer/extend/accompany: a clip from generate_speech. |
| `source_file` | object {download_url, file_id} |  | source track for cover/repaint/extract/add_layer/extend/accompany: an audio file in this conversation. |
| `source_base64` | string |  | source track for cover/repaint/extract/add_layer/extend/accompany: audio bytes, base64. |
| `source_url` | string |  | source track for cover/repaint/extract/add_layer/extend/accompany: a public http(s) link to audio. |
| `reference_music_id` | string |  | Style reference (custom/describe): an earlier song from generate_music. |
| `reference_speech_id` | string |  | Style reference (custom/describe): a clip from generate_speech. |
| `reference_file` | object {download_url, file_id} |  | Style reference (custom/describe): an audio file in this conversation. |
| `reference_base64` | string |  | Style reference (custom/describe): audio bytes, base64. |
| `reference_url` | string |  | Style reference (custom/describe): a public http(s) link to audio. |

#### `get_music` — Check a song *(read-only)*

Status of a song from generate_music — queue position while waiting; once it has succeeded, the audio link, the final lyrics and the bpm/key it was made in.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `music_id` | string | **yes** | e.g. mus_1a2b… |

#### `list_music` — List songs *(read-only)*

The signed-in person's 30 most recent songs from generate_music, newest first.

*No arguments.*

### Personal voices

#### `list_voices` — List my voices *(read-only)*

The person's own trained voices (made from their own recordings), with status, how much audio they have, and the measured voice profile (range, vibrato, tone). Voices are private to their owner and are kept until deleted.

*No arguments.*

#### `get_voice` — Get a voice *(read-only)*

One voice: status, training progress, profile.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `voice_id` | string | **yes** |  |

#### `get_voice_upload_link` — Link to add recordings *(read-only)*

A page where the PERSON creates a voice and uploads or records their own voice in their browser. Give them the URL. Never upload someone's voice for them.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `voice_id` | string |  | omit to create a new voice |

#### `train_voice` — Train a voice

Start training once the person has uploaded at least 5 minutes of singing (10-30 sounds fuller). Runs in slices on the shared GPU (20-40 min). Poll get_voice.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `voice_id` | string | **yes** |  |
| `quality` | string — one of: `standard`, `high` |  |  |

#### `resing_song` — Change the singer to my voice

Re-sing a song's lead vocal in the person's trained voice and mix it back onto the original music. key=auto moves the vocal by octaves into their range. It keeps the original melody and phrasing. Give the song as one source_*.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `voice_id` | string | **yes** |  |
| `key` | string |  | auto, or semitones -24..24 |
| `keep_backing_vocals` | boolean |  |  |
| `source_music_id` | string |  | a song from generate_music |
| `source_file` | object {download_url, file_id} |  |  |
| `source_url` | string |  | public http(s) audio link |
| `source_base64` | string |  |  |

#### `sing_song` — Sing a new song in my voice

Write (from `description`) or take (`lyrics` + optional `style`) a song, perform it in the person's range and manner from their voice profile, and sing it in their trained voice. About 1-2 minutes when the GPU is free.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `voice_id` | string | **yes** |  |
| `description` | string (max length 1000) |  |  |
| `lyrics` | string (max length 4000) |  |  |
| `style` | string (max length 1000) |  |  |
| `language` | string |  |  |
| `duration_s` | number (≥ 10, ≤ 360) |  |  |
| `seed` | integer (≥ 0) |  |  |
| `use_style_model` | boolean |  | perform it with the person's trained singing style when they have one (default true) |
| `style_strength` | number (≥ 0, ≤ 1) |  | how strongly (default 0.8) |

#### `speak_as_voice` — Speak text in my voice

Say text in the person's trained voice. Burmese (Myanmar) text works too: it is detected automatically (or pass language=Burmese) and spoken by the Burmese engine before the person's timbre is applied.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `voice_id` | string | **yes** |  |
| `text` | string (max length 2000) | **yes** |  |
| `language` | string |  |  |
| `delivery` | string |  | e.g. calm and slow |

#### `get_voice_job` — Check a voice job *(read-only)*

A resing/song/speech job: when it has succeeded, audio_url and stems (links work for 7 days; files are deleted after 90 days).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `job_id` | string | **yes** |  |

---

## Part C — Personal voices: REST `/v1` API (layers 1–3)

This is the full voice specification (source: `docs/VOICE_API_SPEC.md`). Its §-numbers are its own.


### Contents

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

### 1. What the product does

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

### 2. Conventions

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

### 3. Authentication

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

### 4. Jobs: the async model every endpoint uses

Everything that touches the GPU is **asynchronous**. The single GPU (RTX 4090, 24 GB) is shared with
the studio's picture/video service, songs and speech. Work waits in **one queue**. A create call returns
immediately with a job; the client polls, or registers a webhook.

#### 4.1 Job object

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

#### 4.2 Endpoints

| Method & path | Description |
|---|---|
| `GET /v1/jobs/{job_id}` | One job. Poll every **5 s** while `running`, every **20 s** while `queued`. |
| `GET /v1/jobs?status=&kind=&voice_id=` | The caller's jobs, newest first. |
| `POST /v1/jobs/{job_id}/cancel` | Cancel. `queued` → `cancelled` at once. `running` stops at the next safe point (training stops after the current slice and keeps its progress). Returns the job. |

#### 4.3 Webhooks (optional)

Any create call may include `"callback_url": "https://…"`. On every status change the server POSTs the
job object there. The body is signed: header `X-Studio-Signature: t=<unix>,v1=<hex HMAC-SHA256 of
"<t>.<body>">`, keyed by a per-client webhook secret **[new]**, returned as `webhook_secret` by
`POST /oauth/register`. Expect retries
(1 m, 5 m, 30 m); respond 2xx within 10 s. Polling must still work — webhooks are a convenience.

#### 4.4 Typical times (measured on this GPU when it is free)

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

### 5. Voices — training a personal voice (layers 1 + 2)

#### 5.1 Lifecycle

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

#### 5.2 Voice object

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

#### 5.3 Endpoints

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

#### 5.4 Clip object

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

#### 5.5 Recording guidance (show this in the app)

- Quiet room, no echo; phone 20–30 cm from the mouth; no auto-tune or effects.
- Sing songs you know well, across your range: low, middle, high, soft and loud.
- 10 short songs beat one long one. Include some speaking if you'll use speech.

#### 5.6 Profile object (layer 2)

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

### 6. Style models — learning how a person sings (layer 3)

A style model is a small adapter for the song model (ACE-Step 1.5, LoKr) trained on **full songs the person
sang, with their lyrics**. With it attached, new songs are *performed* with their phrasing and
ornamentation before the voice model gives them their exact sound.

It belongs to one voice. Creating a style model does not change the voice.

#### 6.1 Lifecycle

`collecting` → (≥ **3** songs with confirmed lyrics and ≥ **8 minutes**; 10–20 songs recommended) → `ready_to_train` → `training` → `ready` | `failed`

**As built:** training runs **whole, not in slices**. It starts as soon as WIS is idle and 21 GB of the card is free, at any hour (the box is dedicated; `STUDIO_STYLE_TRAIN_WINDOW` can restrict start hours if ever needed), on the separate `music-train` engine. Epochs are sized to a ~90-minute run, from 80 to 300. A `style_train` job may wait up to 72 h. Drafted lyrics come from Whisper on the CPU; a draft that loops ("hmm, hmm…") is discarded and the person is asked to type the words. The trained adapter (~15 MB) is kept; the uploaded songs expire after 90 days.

#### 6.2 StyleModel object

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

#### 6.3 Endpoints

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

### 7. Using a voice

Every endpoint in §7 requires the voice's `status` to be `ready` and the caller to **own** it (`404`
otherwise — never `403`, so voice IDs don't leak). Each creates a Job (§4). Each counts toward the hourly
limits in §9.

#### 7.1 Change the singer — `POST /v1/voices/{id}/resing`

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

#### 7.2 Sing a new song — `POST /v1/voices/{id}/songs`

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

#### 7.3 Speak text — `POST /v1/voices/{id}/speech`

```json
{ "text": "Once upon a time…",                 // 1..2000 chars
  "language": "en",                             // optional; default auto
  "delivery": "calm and slow",                  // optional speaking-style hint
  "callback_url": "optional" }
```

Stages: `speaking` → `converting`. `result`: `Audio`.

#### 7.4 Re-sing / convert a short vocal — `POST /v1/voices/{id}/convert`

For apps that already have a dry vocal (e.g. the person hums a melody, or another singer's a-cappella).
No separation and no mixing.

```json
{ "source": { "upload_id": "upl_…" }, "key": "auto", "content": "singing" }  // singing | speech
```

#### 7.5 Put a result in a video project — `POST /v1/projects/{project_id}/audio`

**[new; uses the existing project model]**

```json
{ "job_id": "vjob_…" }   // or "music_id" / "speech_id" [exists]
```

Returns `{"project_id": "prj_…", "files": n}`. In a storyboard it becomes the soundtrack. In single-clip
mode it becomes a reference audio (the clip model takes up to 3).

#### 7.6 Uploads — `POST /v1/uploads`

multipart `file` → `201 {"upload_id": "upl_…", "duration_s": 213.4, "expires_at": …}`. Kept 24 h. Use
for any `source.upload_id`. Up to 100 MB.

#### 7.7 Existing endpoints a voice app will also want **[exists, via MCP today; to be mirrored at `/v1`]**

| Capability | MCP tool | Mirror at |
|---|---|---|
| Make a song (describe / lyrics / cover / repaint / extract / add_layer / extend / accompany) | `generate_music`, `get_music`, `list_music` | `POST /v1/music`, `GET /v1/music/{id}`, `GET /v1/music` |
| Speech with preset speakers or a quick clone (no training) | `generate_speech`, `get_speech`, `list_speech` | `POST /v1/speech`, … |
| Pictures | `generate_image`, `get_image`, `list_images` | `POST /v1/images`, … |
| Video projects | `create_project`, `update_project`, `add_file`, `render_video`, `get_status`, … | `/v1/projects/…` |

`/v1` mirrors take the same arguments as the MCP tool's input schema (`GET /mcp` → `tools/list` gives the
authoritative JSON Schema for each) and return the same objects.

---

### 8. MCP tools

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

### 9. Limits, errors, privacy

#### 9.1 Limits (per account; admin can mark an account unlimited)

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

#### 9.2 Errors

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

#### 9.3 Consent and privacy (product rules — the backend enforces them)

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

### 10. Backend implementation notes

For the person building the server side. Everything here was checked against this box on 2026-10-25.

#### 10.1 Topology

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

#### 10.2 GPU queue rules (extend `app/gpuqueue.py`)

- Add kinds with `NEED_MB` (measure; starting values): `rvc_convert` 6000, `rvc_train` 10000,
  `style_train` 20000 (upstream says ~17 GB), `song` 14000 (the existing music value).
- A job starts only when WIS reports an empty queue, no studio video render is running, and free VRAM ≥ NEED.
  Already implemented for speech/music; reuse it.
- **Training is sliced:** each slice is one queue job of about 5 minutes; then the GPU is released and the next
  slice re-queues **behind** anything that arrived meanwhile. Never hold the GPU for a whole training run.
- An out-of-memory error → requeue (max 4), never fail someone else's job.
- Multi-stage generation jobs (`song` = ACE-Step then RVC) hold their place: stage 2 runs as the same queue
  job right after stage 1.

#### 10.3 Voice training pipeline (Ultimate RVC 0.6 core API)

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

#### 10.4 Conversion pipeline

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

#### 10.5 ACE-Step rules learned the hard way (all verified)

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

#### 10.6 Style model training (LoKr)

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

#### 10.7 Data model (sqlite, new tables)

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

#### 10.8 House rules for this codebase

- Put business rules in a module (`app/voices.py`) that both the `/v1` handlers and the MCP tools call, never
  in a handler. Otherwise one front door skips a rule.
- Test changes on a throwaway web container (`-p 127.0.0.1:8121:8120 --network story-studio_default`,
  then `docker network connect wis_default <name>`; `STUDIO_COOKIE_SECURE=0`, a scratch `/data` holding a
  `sqlite3 .backup` copy of the DB). Then run `docker compose up -d --build web`, but only when no
  project render is running, because the render worker runs inside the web process.
- Don't touch other services on the box. Containers are `restart: unless-stopped` and run as `user: 1000:1000`.

---

### 11. Acceptance tests

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

### 12. Open questions

1. ~~Clip retention~~ **Decided:** clips are deleted 90 days after upload, like everything else; the trained
   model and profile are kept until the voice is deleted.
2. **Shift on re-sing:** for a non-octave key shift, move only the vocal (it may clash with the music)
   or the whole song (the music changes key)? Proposed: an octave move only by default; a non-octave
   shift moves the whole song and is opt-in.
3. **LoKr slicing:** **answered: it can't resume** (§10.6). Choose between patching upstream for resume and
   an exclusive overnight window, once the pilot has measured GPU memory and time.
4. **Disk:** 844 GB free after the 2026-10-25 cleanup. A trained voice takes about 120 MB, and its cleaned
   clips about 75 MB until they expire.
