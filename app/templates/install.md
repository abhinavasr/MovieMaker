# Install Story Studio in an AI assistant

Story Studio turns pictures plus a story into a video. This page tells an AI assistant
(Claude Code, Claude, ChatGPT, or anything else that speaks MCP) how to connect to it, so a
person can say "regenerate my video" instead of clicking through the website.

- **MCP server URL:** `{{ base }}/mcp`
- **Transport:** Streamable HTTP
- **Sign-in:** OAuth, in the browser, with a Story Studio username and password. Accounts are
  issued by the studio's administrator and expire; there is no sign-up. Never ask the person
  to paste their password into the chat — the sign-in page is the only place it goes.

## If you are Claude Code

Run this yourself:

```bash
claude mcp add --transport http --scope user story-studio {{ base }}/mcp
```

Then tell the person, in these words: **"Type `/mcp`, pick `story-studio`, choose
Authenticate, and sign in with your Story Studio account in the browser window that opens."**
You cannot do that step for them. The tools appear once they have signed in; if they do not
show up in the current session, the person should start a new one.

## If you are ChatGPT

You cannot add a connector yourself; the person does it once, and it needs a plan that offers
developer mode. Give them these steps:

1. Open **Settings → Apps & Connectors → Advanced settings** and turn on **Developer mode**.
2. Back in **Apps & Connectors**, choose **Create**.
3. Name: `Story Studio`. MCP server URL: `{{ base }}/mcp`. Authentication: **OAuth**.
4. Tick the box saying you trust the application, press **Create**, and sign in with the Story
   Studio account when the window opens.
5. In a chat, enable the Story Studio connector from the **+** / tools menu.

The menu names move between ChatGPT versions; what matters is "add a custom MCP connector"
with the URL above and OAuth.

## If you are Claude (claude.ai or the desktop app)

The person adds it under **Settings → Connectors → Add custom connector**, with the name
`Story Studio` and the URL `{{ base }}/mcp`, then presses **Connect** and signs in.

## Anything else

Any MCP client that supports remote servers over Streamable HTTP with OAuth works. Point it
at `{{ base }}/mcp`; it will discover the rest from
`{{ base }}/.well-known/oauth-protected-resource`.

## Building your own app

The full API spec, for developers, is at `{{ base }}/dev`. It documents every tool and the REST
voice API, with downloadable Markdown. In short:

- **Browser and mini apps:** CORS is on for `/mcp`, `/v1` and `/oauth`, any origin including a
  sandboxed `Origin: null`. Send `Authorization: Bearer …`; cookies are never accepted
  cross-origin.
- **An app that can't redirect or open a pop-up** (a sandboxed mini app, a TV, a script) signs
  in with **device sign-in** (RFC 8628). It shows a code, the person approves it at
  `{{ base }}/activate`, and the app polls `/oauth/token`.

## What you can do once connected

A project is made one of two ways:

- **storyboard — with scenes.** One picture per scene, each animated for a few seconds and
  joined into one film. Each scene has its own prompt describing what moves and how the
  camera moves. Silent unless a soundtrack was uploaded.
- **single — without scenes.** One clip of up to ~15 seconds from the story as the prompt,
  with up to 9 reference pictures (`<Picture 1>`, `<Picture 2>`… in the prompt) and generated
  sound.

`update_project` can switch a project from one to the other, so the same pictures and story
can be regenerated either way.

| Tool | What it does |
|---|---|
{% for name, description in tools -%}
| `{{ name }}` | {{ description }} |
{% endfor %}
**To regenerate a video:** `list_projects` → `get_project` → change the story
(`update_project`) or the scene prompts (`set_scenes`) → `render_video` → `get_status` every
20–30 seconds until `succeeded` → give the person the `video_url`.

In a storyboard, `render_video` with `scenes: [2]` regenerates only scene 2 and keeps the
other clips.

**Pictures:** `generate_image` makes a still from a prompt (text-to-image), or from up to 4
source pictures plus a prompt saying what to change or combine (image-to-image). Sources can
be a file in the chat, a public URL, an earlier `image_id`, or a project's pictures. It
usually returns the finished picture within the call; otherwise poll `get_image`. To refine a
result, pass its `image_id` back as a source. To put one in a video, `add_file` with
`image_id`.

**Speech:** `generate_speech` reads text aloud with Qwen3-TTS — one of nine preset speakers,
optionally steered with `instruct` ("calm and slow"), or a clone of a voice clip you pass with
its transcript. Burmese (Myanmar) is spoken by its own four voices (Thiri, Nandar, Kyaw, U_Ba) or a
clone, and Burmese script is detected automatically. Hindi can use four native Hindi voices (Meera, Anaya,
Arjun, Vikram; Meera is the default for Devanagari text) or any of the nine Qwen voices, which speak it with an accent. Sanskrit ślokas can be CHANTED: pass
language=Sanskrit (Vāgdhenu; the metre is detected, or choose it with `meter`). Up to 1200 characters per clip. It usually returns within the call; otherwise
poll `get_speech`. Give the person the `audio_url`; to make it a storyboard soundtrack,
`add_file` with `speech_id`.

**Music:** `generate_music` (ACE-Step 1.5) makes full songs with vocals. `task: describe` takes
one sentence and writes the lyrics itself; `task: custom` takes your `lyrics` (with [Verse] /
[Chorus] tags on their own lines) and a `style`, or `instrumental: true`, plus an optional
`reference_*` track for the sound. `cover`, `repaint`, `extract`, `add_layer` and `extend`
work on a `source_*` track (an earlier `music_id`, a file, or a URL). Poll `get_music`; it
returns the audio link and the final lyrics. `add_file` with `music_id` makes it a soundtrack.

**Keep your own copies:** everything made or uploaded is deleted 90 days after it last changed;
each result carries `deleted_on`. Download what you want to keep. Trained voices and voice profiles
are kept until deleted.

Speech and music share one GPU queue with pictures and videos: while the card is busy, a
request waits and the reply carries `queue_position`. Do not resubmit — poll instead.

Rendering runs on one GPU, one project at a time, at roughly 2 minutes per 5 seconds of
video, and each account has an hourly render limit — so do not start a render the person did
not ask for. Getting pictures in: an image that is in the conversation — one you generated, or one the
person attached — goes in with `add_file`. A public link goes in with `add_media`. A file on
disk that you can reach with a shell goes in with `get_upload_link`, which returns a ready
`curl` command. Failing all three, send the person to the project page.
