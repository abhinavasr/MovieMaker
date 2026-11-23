# Story Studio

A small web front end onto the WIS video API (`~/wis`), so people who do not have an API key
can turn **pictures plus a story** into a video. Accounts are issued by an admin and expire.
There is no sign-up.

- **Local:** `http://127.0.0.1:8120`
- **Public:** whatever hostname you route the Cloudflare tunnel to
- **Upstream:** `http://wis-api:8090` over the existing `wis_default` docker network

Everything runs in Docker, has its own compose project, its own network and an unused port,
and can be stopped and started at any time without touching the services already on this box.

---

## What the model can actually do

Both modes are the same model — MiniMax H3 — in its two checkpoints. They cannot be resident
at once (19.5 GB each on a 24 GB card), so a project uses one or the other.

| | Storyboard | Single clip |
|---|---|---|
| WIS model | `minimax-h3-i2v` (fl2va) | `minimax-h3-ref2v` (ref2va) |
| Images | one per shot, any number | up to **9**, all conditioning one clip |
| Length | unlimited — shots are joined | up to ~15 s |
| Audio | silent; an uploaded track is laid over the result with ffmpeg | **generated natively** — stereo voice, effects and music |
| Consistency | can drift between shots; "match cuts" reduces it | strong — that is what references are for |
| Cost | ~2 min of GPU per 5 s shot | ~2–5 min |

Sizes offered are the model's native buckets — 480×864, 864×480, 640×640. Bigger is accepted
by WIS with a warning and then usually dies on VRAM part way through, so the UI does not
offer it.

### The `<Picture N>` tags

In single-clip mode the model resolves references **positionally, from tags in the prompt**:
`<Picture 1>` is the first image you uploaded. The tag under each thumbnail tells you which
is which. Referring to them explicitly —

> `<Picture 1>` is the hero. Put `<Picture 1>` on the rooftop from `<Picture 2>` at dusk.

— works markedly better than prose that never names them. If you write no tags at all, a
roster line is prepended for you and WIS returns a `service_note` saying the references were
weakly applied.

---

## Setup

```bash
cd ~/story-studio
cp .env.example .env
```

Fill in two values:

1. **`STUDIO_ADMIN_PASSWORD`** — there is no default; the app refuses to start without it.
2. **`STUDIO_WIS_KEY`** — mint a dedicated key rather than reusing the wallpaper consumer's
   production key, which shares a 60/hour rate bucket and normal scheduling priority:

   ```bash
   cd ~/wis && python3 scripts/manage_keys.py mint --label story-studio --scheduling normal
   ```

Then:

```bash
cd ~/story-studio
docker compose up -d --build
curl -s http://127.0.0.1:8120/healthz | python3 -m json.tool
```

`/healthz` names both models and their state, so a missing checkpoint shows up here rather
than as a 400 somebody hits after waiting in a queue.

### Publishing it

The tunnel container is behind a profile, so it stays down until you ask for it:

```bash
# 1. Cloudflare dashboard -> Zero Trust -> Networks -> Tunnels -> Create a tunnel
# 2. Add a public hostname:  studio.<your-domain>  ->  http://story-studio:8120
#    (or http://127.0.0.1:8120 if you run the tunnel with network_mode: host)
# 3. Paste the tunnel token into STUDIO_TUNNEL_TOKEN in .env
docker compose --profile tunnel up -d tunnel
```

There is a zero-new-container alternative: the existing `wis-tunnel` runs with
`network_mode: host`, so adding a public hostname on **that** tunnel pointing at
`http://127.0.0.1:8120` also works, with nothing new to run. It does mix two services into
one tunnel's lifecycle, which is the reason this stack ships its own.

Keep `STUDIO_COOKIE_SECURE=1` whenever the front door is https.

---

## Using it

**As the admin** — sign in, go to *Users*, issue an account. Leave the password blank and one
is generated and shown **once**; only a hash is kept. Set how many days it is valid for (0 =
never). You can extend, disable, reset or delete an account at any time; disabling and
deleting also drop that person's open sessions immediately.

**As a user** — create a project, pick a mode, upload pictures, write the story, render.

In storyboard mode, press **Plan shots** to split the story into one prompt per picture. That
split is written by the host's Ollama through WIS's `/v1/text/json`; if it is unavailable the
story is split evenly instead and the banner says which you got. Either way you edit the
prompts before rendering — shot prompts should describe **motion and camera**, because the
model already has the picture.

Renders are queued one at a time. That is not a simplification: there is one GPU behind all
of this, so a second concurrent render would not be faster, it would just interleave two
people's waits. Cancelling frees the card immediately.

---

## Using it from Claude or ChatGPT (MCP)

The studio is also an MCP server, so an assistant can edit and regenerate a project for its
owner. People say **"install `<your-hostname>/install`"** to their assistant; that address
serves the instructions as markdown to a program and as a page to a browser.

- **Endpoint:** `/mcp` — Streamable HTTP, stateless, JSON answers (`app/mcp.py`)
- **Sign-in:** OAuth 2.1 with PKCE and dynamic client registration (`app/oauth.py`). The only
  credential is a studio username and password, entered on the studio's own page. Tokens are
  re-checked against the account on every call, and disabling, deleting or resetting an
  account drops its assistant tokens along with its browser sessions.
- **Claude Code:** `claude mcp add --transport http --scope user story-studio https://<host>/mcp`,
  then `/mcp` → Authenticate.
- **ChatGPT / claude.ai:** add a custom connector with that URL and OAuth.

"Scene" in the tools is "shot" in the pages. The two modes are offered as *with scenes*
(`storyboard`) and *without scenes* (`single`), and `update_project` can switch a project
between them so the same pictures and story can be regenerated either way. `render_video`
takes an optional list of scene numbers: those are regenerated and the other clips are reused,
which costs one scene of GPU instead of all of them.

Both front doors call `app/service.py`, so the limits, the "write the story first" refusals
and the hourly render budget are the same whichever one a request came through.

Two kinds of signed, expiring link exist because an assistant cannot carry a browser cookie:
`/v/…/video.mp4` (the finished video, 24 h) and `/u/…` (an upload slot for one project, 1 h,
used with `curl -F files=@picture.png`). `add_media` fetches pictures by URL and refuses any
address that is not public, because the container sits on the same network as the WIS API.

`STUDIO_PUBLIC_URL` pins the address assistants are told to use; left blank it follows the
host each request arrives on, which is right behind the tunnel.

---

## Operating

```bash
docker compose logs -f web          # what it is doing
docker compose restart web          # safe at any time
docker compose down                 # stops only this stack
docker compose --profile tunnel down
```

State lives in `./data` — `studio.db`, `uploads/`, `outputs/`. Projects and their files are
deleted after `STUDIO_RETENTION_DAYS` (14 by default); expired *accounts* are kept so the
admin can still see them, but their password stops working.

The container runs as uid 1000 so nothing in `./data` comes back root-owned.

### Limits worth knowing

| | |
|---|---|
| `STUDIO_USER_RENDERS_PER_HOUR` | 10 per account. A render is real GPU time. |
| `STUDIO_MAX_UPLOAD_MB` | 16 MB per file, matching WIS's own reference limit. |
| `STUDIO_SESSION_TTL_HOURS` | 12. Sessions also die the moment the account expires. |
| `STUDIO_MAX_STORYBOARD_SHOTS` | 12 pictures, i.e. ~25 minutes of GPU for a full render. |
