/* The production editor. One state object from GET /api/editor/<id>, re-read on a
 * timer; every edit is a POST of {changes:[{op,...}]} through the same code the
 * assistant's applied proposals use (app/editor.py apply_changes). */
(() => {
  const ED = document.getElementById("ed");
  const PID = ED.dataset.pid;
  const A = window.AgentChat;
  const esc = A.esc;
  const $ = (id) => document.getElementById(id);
  const TRACK_COLOR = {dialogue: "var(--dlg)", ambience: "var(--amb)", music: "var(--mus)", shot: "var(--sht)"};
  const TRACK_NAME = {dialogue: "Dialogue", ambience: "Ambience", music: "Music", shot: "Shot sound"};
  // The use cases, listed upfront; each one is made by the model that does it best
  // (2026-11-24: "We should use different models for different use cases. and list the use
  // cases upfront"). MAKE[make] = [short label for badges, one-line description].
  const USES = [
    {make: "animate", icon: "🎞", title: "Bring the picture to life", what: "Camera moves and gentle action, starting exactly on the shot's picture. Silent; sound comes from the timeline.", model: "MiniMax H3 image-to-video"},
    {make: "perform", icon: "🧩", title: "Keep characters true to their references", what: "The video also sees the characters' and places' pictures, so faces and places stay consistent; the opening may shift a little. Its own sound is muted.", needs: "characters or places in the shot", model: "MiniMax H3 reference-to-video"},
    {make: "lipsync", icon: "👄", title: "Someone speaks or sings a line", what: "The line is voiced first in their voice; the video is made from that voice, mouth in sync.", needs: "a dialogue line under the shot", model: "Wan2.2-S2V"},
    {make: "longtalk", icon: "🎤", title: "Talk or sing for longer", what: "One person talks or sings a long line or a whole song, mouth in sync, for up to a minute. The voice stays on the timeline.", needs: "the voice or song: a clip on the timeline, or upload it", model: "InfiniteTalk"},
    {make: "talk2", icon: "🗣", title: "Two people talk to each other", what: "Two people in the picture talk, each in their own voice: speaker 1 first, then speaker 2.", needs: "a picture with both people, and each one's voice (a line on the timeline, or upload it)", model: "InfiniteTalk (two speakers)"},
    {make: "motion", icon: "🕺", title: "Copy a dance or movement from a video", what: "The person in the picture makes exactly the movements of a video you add, frame for frame. One person; silent.", needs: "the video to copy", model: "Wan Animate 2"},
    {make: "recast", icon: "🔁", title: "Recast the people in a video", what: "Your characters replace the people in a video — one or several — keeping the video's motion, camera, place and sound. The shot's picture is a frame of the video redrawn with them (🖼 Pictures).", needs: "the video, and who each character replaces", model: "Viggle Remake"},
    {make: "restyle", icon: "🎨", title: "Change a video's look", what: "Change outfits, objects, weather, light, setting or style; the motion, camera and sound stay. The shot's picture is a frame of the video with the change (🖼 Pictures).", needs: "the video, and what changes", model: "Viggle Remake (edit)"},
  ];
  const ICON = Object.fromEntries(USES.map((u) => [u.make, u.icon]));
  // Picture models (2026-11-24: "I should be able to choose models; sometimes the default
  // model may not be generating the correct image").
  const IMG_MODELS = [["", "Auto (the usual one)"], ["flux-2-klein-9b", "FLUX.2 klein — fast"],
    ["qwen-image-2.1-turbo", "Qwen-Image turbo — fast"], ["qwen-image-2.1", "Qwen-Image — careful"],
    ["qwen-image-edit-2511", "Qwen-Image-Edit — keeps identity"]];
  const modelOptions = (cur) => IMG_MODELS.map(([v, t]) => `<option value="${v}" ${v === (cur || "") ? "selected" : ""}>${t}</option>`).join("");
  const MAKE = {animate: ["Bring to life", USES[0].what], perform: ["With references", USES[1].what],
    lipsync: ["Speaks / sings", USES[2].what], longtalk: ["Talks / sings long", USES[3].what],
    talk2: ["Two people talk", USES[4].what], motion: ["Copies motion", USES[5].what],
    recast: ["Recast", USES[6].what], restyle: ["New look", USES[7].what]};
  const FRAMING = {"": "—", EWS: "Extreme wide", WS: "Wide", MS: "Medium", MCU: "Medium close-up", CU: "Close-up", ECU: "Extreme close-up", OTS: "Over the shoulder", POV: "Point of view", "2S": "Two-shot", INSERT: "Insert"};

  let S = null, sig = "", view = "storyboard", tab = "shot", scope = "shot";
  let sel = {scene: null, shot: null, clip: null};
  let genOpen = null, pickedOption = {}, chat = null, preview = null, seenReady = new Set();

  // ------------------------------------------------------------------ util
  const api = A.api;
  function toast(t) { const el = $("toast"); el.textContent = t; el.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(() => el.hidden = true, 2800); }
  const fmt = (s) => { s = Math.max(0, s || 0); const m = Math.floor(s / 60); return m + ":" + (s % 60).toFixed(1).padStart(4, "0"); };
  const amp = (db) => Math.pow(10, db / 20);
  const shotById = (id) => S?.shots.find((s) => s.id === id);
  const sceneById = (id) => S?.scenes.find((s) => s.id === id);
  const refById = (id) => S?.refs.find((r) => r.id === id);
  const clipById = (id) => S?.clips.find((c) => c.id === id);
  const sceneShots = (sid) => (sceneById(sid)?.shots || []).map(shotById).filter(Boolean);
  const sceneSpan = (sid) => { const sh = sceneShots(sid); if (!sh.length) return [0, 0]; return [sh[0].start, sh[sh.length - 1].start + sh[sh.length - 1].seconds]; };
  async function change(changes, msg) {
    try { const r = await api(`/api/editor/${PID}/changes`, {changes}); if (msg) toast(msg); await load(true); return r; }
    catch (e) { toast(e.message); await load(true); throw e; }
  }
  const typing = () => { const a = document.activeElement; return a && (a.tagName === "TEXTAREA" || (a.tagName === "INPUT" && a.type !== "range" && a.type !== "checkbox")) && a.closest("#pane-shot, .ed-pop, #v-assets, .ed-sheet"); };

  // ------------------------------------------------------------------ load
  let timer = null;
  async function load(force) {
    clearTimeout(timer);
    try {
      const st = await api(`/api/editor/${PID}`);
      // Signed links carry an expiry; they must not count as a change, or the whole editor
      // is rebuilt every poll and clicks land on buttons that were just replaced.
      const s2 = JSON.stringify({...st, now: 0, deleted_on: 0, days_left: 0}).replace(/\/\d{9,11}\/[0-9a-f]{12,}\//g, "/~/");
      const changed = s2 !== sig;
      S = st; sig = s2;
      if (!sel.scene || !sceneById(sel.scene)) sel.scene = S.scenes[0]?.id || null;
      if (sel.shot && !shotById(sel.shot)) sel.shot = null;
      if (!sel.shot) sel.shot = sceneShots(sel.scene)[0]?.id || null;
      if (sel.clip && !clipById(sel.clip)) sel.clip = null;
      if (changed || force) paint();
      else { paintRender(); paintStopAI(); if (S.proposals.some((p) => p.options.some((o) => !["succeeded", "failed", "gone"].includes(o.status)))) { if (!Player.playing && view !== "assets") paintStage(); if (!typing()) paintInspector(); } }
    } catch (e) { console.warn(e); }
    const busy = S && (["running", "queued"].includes(S.status) || S.shots.some((s) => s.auto) || S.clips.some((c) => c.status === "pending" || (c.status === "script" && c.auto)) || S.export.status === "running" || S.proposals.some((p) => p.options.some((o) => !["succeeded", "failed", "gone"].includes(o.status))));
    timer = setTimeout(load, busy ? 2000 : 6000);
  }

  function paint() {
    if (document.activeElement !== $("ptitle")) $("ptitle").value = S.title;
    if (document.activeElement !== $("aspect")) $("aspect").value = S.aspect;
    paintStory(); paintScenes(); paintRefs(); paintRender(); paintStopAI();
    if (view === "assets") paintAssets();
    else { paintCrumbs(); if (!Player.playing) paintStage(); paintStrip(); }
    paintTimeline();
    if (!typing()) paintInspector();
    const n = S.proposals.length; $("prop-n").hidden = !n; $("prop-n").textContent = n;
    paintPropbar();
    $("applies").checked = S.agent_applies;
    const d = String(S.mix.duck_db <= -0.5 ? Math.round(S.mix.duck_db) : 0);
    if (![...$("duck").options].some((o) => o.value === d)) $("duck").insertAdjacentHTML("beforeend", `<option value="${d}">${d} dB</option>`);
    $("duck").value = d;
  }

  // ------------------------------------------------------------------ top bar
  $("ptitle").addEventListener("change", async () => { await api(`/api/editor/${PID}/settings`, {title: $("ptitle").value}); toast("Renamed"); load(true); });
  document.querySelectorAll(".ed-views button").forEach((b) => b.onclick = () => setView(b.dataset.view));
  function setView(v) {
    view = v;
    document.querySelectorAll(".ed-views button").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
    $("v-storyboard").style.display = v === "assets" ? "none" : "contents";
    $("v-assets").hidden = v !== "assets";
    $("tl").style.display = v === "assets" ? "none" : "";
    if (v === "timeline") { $("v-storyboard").querySelector(".ed-strip").style.display = "none"; }
    else $("v-storyboard").querySelector(".ed-strip").style.display = "";
    $("tl-title").textContent = v === "timeline" ? "Film timeline" : "Scene timeline";
    paint();
  }
  function paintRender() {
    const box = $("render-state");
    const done = S.shots.filter((s) => s.status === "done").length;
    if (["running", "queued"].includes(S.status)) {
      // One Stop button (top bar) stops this render and every other AI job; no separate
      // render-only Cancel, which left pictures and voicing running.
      box.innerHTML = `<span class="bar"><i style="width:${S.progress}%"></i></span><span title="${esc(S.message)}">${esc(S.status === "queued" ? "waiting for the GPU" : S.message || "rendering")}</span>`;
    } else box.innerHTML = S.shots.length ? `<span>${done}/${S.shots.length} shots rendered</span>` : "";
    const busy = ["running", "queued"].includes(S.status);
    const needPic = S.shots.filter((s) => !s.asset_id && s.start_from !== "prev_last" && s.needs_picture).length;
    const canVideo = S.shots.filter((s) => s.status !== "done" && (s.asset_id || s.start_from === "prev_last" || !s.needs_picture)).length;
    const badge = (id, n) => { const b = $(id).querySelector(".n"); b.hidden = !n; b.textContent = n || ""; };
    $("draw-missing").disabled = !S.shots.length;
    badge("draw-missing", needPic);
    $("render-missing").disabled = !S.shots.length || busy || !canVideo;
    badge("render-missing", canVideo);
    const ex = S.export;
    const b = $("export");
    b.disabled = ex.status === "running";
    b.querySelector(".t").textContent = ex.status === "running" ? "Mixing…" : "Export";
    b.classList.toggle("ready", !!ex.url && ex.status !== "running");
  }
  // Two separate steps (the person's rule): pictures, then -- only when asked -- videos.
  // Making videos never draws pictures, and drawing pictures never starts the video model.
  async function renderShots(ids, what) {
    try {
      const r = await api(`/api/editor/${PID}/render`, Object.assign({step: "videos"}, ids ? {shot_ids: ids} : {}));
      if (!r.rendering) toast(r.need_pictures ? `${r.need_pictures === 1 && ids?.length === 1 ? "This shot needs" : r.need_pictures + " shot(s) need"} a picture first — use 🖼 Draw pictures` : "Nothing to render");
      else toast(`Making ${what || r.rendering + " video(s)"}` + (r.need_pictures ? ` · ${r.need_pictures} shot(s) skipped: no picture yet` : ""));
      load(true); return r;
    } catch (e) { toast(e.message); }
  }
  async function drawPictures(ids) {
    try {
      const r = await api(`/api/editor/${PID}/render`, Object.assign({step: "pictures"}, ids ? {shot_ids: ids} : {}));
      toast(r.making_pictures ? `Drawing pictures for ${r.making_pictures} shot(s) — characters and places first. No videos until you press 🎬 Make videos.` : "Every shot already has its picture");
      load(true); return r;
    } catch (e) { toast(e.message); }
  }
  $("render-missing").onclick = () => renderShots(null, "the videos");
  $("draw-missing").onclick = () => drawPictures(null);
  function aiSummary() {
    const a = S.ai || {}, parts = [];
    if (a.rendering) parts.push(`rendering (${a.render_message || "queued"})`);
    if (a.pictures) parts.push(`${a.pictures} picture(s) being made`);
    if (a.shots_waiting) parts.push(`${a.shots_waiting} shot(s) being prepared`);
    if (a.voicing) parts.push(`${a.voicing} line(s) being voiced`);
    if (a.agent) parts.push(`the agent (${a.agent})`);
    return parts;
  }
  function paintStopAI() {
    const parts = aiSummary();
    $("stop-ai").hidden = !parts.length;
    $("stop-ai").title = parts.length ? "Stop: " + parts.join(" · ") : "";
  }
  $("free-gpu").onclick = async () => { try { const d = await api(`/api/editor/${PID}/free-gpu`, {}); toast(d.message); } catch (e) { toast(e.message); } };
  $("stop-ai").onclick = () => {
    const parts = aiSummary();
    modal(`<h2>Stop all AI work in “${esc(S.title)}”?</h2>
      <ul style="margin:0;padding-left:18px;font-size:.9rem">${parts.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>
      <p class="ed-note" style="font-size:.84rem">Stops it all now, including the GPU jobs. Finished pictures, clips and voices are kept; lines not yet voiced stay as script lines to voice later.</p>
      <div class="acts"><button class="ed-btn ghost" data-act="close">Keep going</button><button class="ed-btn danger" data-act="stop">■ Stop everything</button></div>`,
      async (act) => {
        if (act !== "stop") return true;
        try { const d = await api(`/api/editor/${PID}/stop-all`, {});
          toast("Stopped" + [d.render && " the render", d.pictures && ` ${d.pictures} picture(s)`, d.voicing && ` ${d.voicing} line(s)`, d.agent && " the agent"].filter(Boolean).join(",")); load(true);
        } catch (e) { toast(e.message); }
        return true;
      });
  };
  $("export").onclick = () => openExport();
  const more = (open) => { $("more-menu").hidden = !open; $("more-btn").setAttribute("aria-expanded", String(open)); };
  $("more-btn").onclick = (e) => { e.stopPropagation(); more($("more-menu").hidden); };
  document.addEventListener("click", (e) => { if (!e.target.closest(".ed-more")) more(false); });
  $("del-prod").onclick = () => modal(`<h2>Delete “${esc(S.title)}”?</h2>
      <p style="font-size:.88rem">This deletes the whole production: ${S.scenes.length} scene(s), ${S.shots.length} shot(s) and their clips, the timeline audio, references, proposals, the exported film and the conversation with the assistant. It can't be undone.</p>
      <p class="ed-note">Pictures, speech and songs also saved in your studio library stay there.</p>
      <label class="ed-field">Type the title to confirm<input id="del-confirm" autocomplete="off" placeholder="${esc(S.title)}"></label>
      <div class="acts"><button class="ed-btn ghost" data-act="close">Cancel</button><button class="ed-btn danger" data-act="delete" id="del-go" disabled>Delete production</button></div>`,
    async (act) => {
      if (act !== "delete") return true;
      try { await api(`/api/editor/${PID}/delete`, {}); location.href = "/edit"; } catch (e) { toast(e.message); return false; }
      return true;
    });
  document.addEventListener("input", (e) => { if (e.target.id === "del-confirm") $("del-go").disabled = e.target.value.trim() !== S.title.trim(); });

  function openExport() {
    const ex = S.export;
    const missing = S.shots.filter((s) => !s.clip), stale = S.shots.filter((s) => s.clip && s.status !== "done");
    modal(`<h2>Mix &amp; export</h2>
      <p class="ed-note" style="font-size:.84rem">Joins the shots' clips as they are and mixes the timeline over them — dialogue, ambience and music with their levels, fades and ducking. It never runs the video model: a quick mix on the CPU.</p>
      ${missing.length ? `<p style="color:var(--warn);font-size:.84rem">${missing.length} shot(s) have no clip yet and appear as their still picture: ${missing.slice(0, 6).map((s) => esc(s.title || "shot " + s.n)).join(", ")}${missing.length > 6 ? "…" : ""}.</p>` : ""}
      ${stale.length ? `<p class="ed-note" style="font-size:.8rem">${stale.length} shot(s) changed since they were rendered; their existing clip is used.</p>` : ""}
      ${ex.status === "failed" ? `<p style="color:var(--bad);font-size:.84rem">Last export failed: ${esc(ex.error)}</p>` : ""}
      ${ex.url ? `<video src="${esc(ex.url)}" controls style="width:100%;max-height:50dvh;border-radius:10px;background:#000"></video>` : ""}
      <div class="acts">
        ${ex.url ? `<a class="ed-btn" href="${esc(ex.url)}&download=1">⤓ Download MP4</a>` : ""}
        ${ex.url && window.BATIYAO_ON ? `<button class="ed-btn" type="button" data-bt-share="film:${PID}">Share to Batiyao</button>` : ""}
        ${missing.length ? `<button class="ed-btn" data-act="render" title="Uses the video model, for shots that have a picture">🎬 Make ${missing.length} missing video(s) first</button>` : ""}
        <button class="ed-btn gold" data-act="mix">${ex.url ? "Mix again" : "Mix the film"}</button>
        <button class="ed-btn ghost" data-act="close">Close</button></div>`, async (act) => {
      if (act === "render") { await renderShots(null, "the missing shots"); return true; }
      if (act === "mix") { try { await api(`/api/editor/${PID}/export`, {}); toast("Mixing the film…"); load(true); } catch (e) { toast(e.message); } return true; }
      return true;
    });
  }

  // ------------------------------------------------------------------ left: story
  function paintStory() {
    const tx = (S.story || "").replace(/[#*_`>]/g, "").trim();
    $("story-card").innerHTML = (tx ? `<div class="tx">${esc(tx.slice(0, 600))}</div>` : `<div>No story yet. Write or paste it here, or develop it with the agent.</div>`)
      + `<div class="acts"><button class="ed-btn sm" id="story-build" type="button" title="The agent builds the scenes and shots from the story">✨ Build shots</button>${S.shots.length && !S.clips.some((c) => c.track === "dialogue") ? `<button class="ed-btn sm gold" id="story-lines" type="button" title="The agent puts the story's dialogue and narration under the shots, then they are voiced">📝 Add lines</button>` : ""}</div>`;
    if ($("story-lines")) $("story-lines").onclick = (e) => { e.stopPropagation(); addLines(); };
    $("story-build").onclick = (e) => { e.stopPropagation(); if (!tx) { storyModal(); return; }
      scope = "story"; paintScope(); setTab("agent");
      chatSend("Define all the scenes and their shots."); };
  }
  function addLines() {
    scope = "story"; paintScope(); setTab("agent");
    chatSend("Add the script's lines: put every line of dialogue and narration from the story under the shot where it is said, word for word, with its speaker (add_lines), and voice them.");
  }
  function storyModal() {
    modal(`<h2>Story</h2><p class="ed-note">The agent reads this for every scene and shot. Markdown is fine.</p>
      <textarea class="ed-story" id="story-text" placeholder="The story, the characters, the scenes…">${esc(S.story || "")}</textarea>
      <div class="acts"><button class="ed-btn ghost" data-act="close">Cancel</button><button class="ed-btn" data-act="save">Save</button><button class="ed-btn gold" data-act="build">Save & build scenes and shots</button></div>`,
      async (act) => {
        if (act === "close") return true;
        try { await api(`/api/editor/${PID}/settings`, {story: $("story-text").value}); toast("Story saved"); load(true); } catch (e) { toast(e.message); return false; }
        if (act === "build") { scope = "story"; paintScope(); setTab("agent"); chatSend("Define all the scenes and their shots."); }
        return true;
      });
    $("sheet").classList.add("wide"); setTimeout(() => $("story-text")?.focus(), 30);
  }
  $("story-edit").onclick = storyModal;
  $("story-card").addEventListener("click", (e) => { if (!e.target.closest("button")) storyModal(); });

  // ------------------------------------------------------------------ left: scenes + refs
  function paintScenes() {
    $("scenes").innerHTML = S.scenes.map((sc, i) => {
      const shots = sceneShots(sc.id), pic = shots.find((s) => s.picture)?.picture;
      const cast = (sc.refs || []).map(refById).filter(Boolean);
      return `<div class="ed-scene ${sc.id === sel.scene ? "on" : ""}" data-scene="${sc.id}" draggable="true">
        <div class="th">${pic ? `<img src="${esc(pic)}" alt="" loading="lazy">` : "🎞"}</div>
        <div><b>${String(i + 1).padStart(2, "0")}. ${esc(sc.title || "Untitled scene")}</b><span>${shots.length} shot${shots.length === 1 ? "" : "s"} · ${Math.round(shots.reduce((a, s) => a + s.seconds, 0))} s ${sc.locked ? '<span class="lock" title="Locked: the agent won\'t change it">🔒</span>' : ""}</span>
          ${cast.length ? `<div class="cast">${cast.slice(0, 8).map((r) => r.image ? `<img src="${esc(r.image)}" alt="${esc(r.name)}" title="${esc(r.name)}">` : `<i title="${esc(r.name)}">${esc(r.name[0] || "?")}</i>`).join("")}</div>` : ""}</div>
        <button class="sc-edit" type="button" data-scedit="${sc.id}" title="Edit this scene">✎</button></div>`;
    }).join("");
  }
  $("scenes").addEventListener("click", (e) => {
    const ed = e.target.closest("[data-scedit]"); if (ed) { e.stopPropagation(); sceneModal(sceneById(ed.dataset.scedit)); return; }
    const d = e.target.closest("[data-scene]"); if (!d) return; selectScene(d.dataset.scene); ED.classList.remove("left-open"); if (phone()) setM("stage"); });
  function sceneModal(sc) {
    if (!sc) return;
    const on = new Set(sc.refs || []);
    modal(`<h2>Scene ${S.scenes.indexOf(sc) + 1}</h2>
      <label class="ed-field">Title<input id="sm-title" value="${esc(sc.title)}" maxlength="120"></label>
      <label class="ed-field">What happens (the agent reads this for every shot in the scene)<textarea id="sm-sum" rows="5">${esc(sc.summary)}</textarea></label>
      <div class="ed-field">Assets in this scene — new shots get these as references<div class="ed-pick">${S.refs.map((r) => `<label class="ed-chip ${on.has(r.id) ? "" : "off"}" style="cursor:pointer"><div class="th">${r.image ? `<img src="${esc(r.image)}" alt="">` : "👤"}</div><span>${esc(r.name)}</span><input type="checkbox" data-scref="${r.id}" ${on.has(r.id) ? "checked" : ""} style="accent-color:var(--gold)"></label>`).join("") || '<span class="ed-note">No references yet — add characters and places under Project references.</span>'}</div></div>
      <label class="ed-switch"><input type="checkbox" id="sm-lock" ${sc.locked ? "checked" : ""}> 🔒 Lock this scene — the agent won't change it or its shots</label>
      <div class="acts"><button class="ed-btn danger" data-act="delete" style="margin-right:auto">Delete scene</button><button class="ed-btn ghost" data-act="close">Cancel</button><button class="ed-btn gold" data-act="save">Save</button></div>`,
      async (act) => {
        if (act === "close") return true;
        if (act === "delete") { if (!confirm(`Delete “${sc.title}” and its ${sc.shots.length} shot(s)?`)) return false; await change([{op: "delete_scene", scene_id: sc.id}], "Scene deleted"); return true; }
        const refs = [...document.querySelectorAll("[data-scref]")].filter((x) => x.checked).map((x) => x.dataset.scref);
        await change([{op: "edit_scene", scene_id: sc.id, title: $("sm-title").value, summary: $("sm-sum").value, refs, locked: $("sm-lock").checked}], "Scene saved");
        return true;
      });
    document.querySelectorAll("[data-scref]").forEach((x) => x.onchange = () => x.closest(".ed-chip").classList.toggle("off", !x.checked));
  }
  dragList($("scenes"), "[data-scene]", (id, toIndex) => change([{op: "edit_scene", scene_id: id, position: toIndex}], "Scene moved"));
  $("add-scene").onclick = async () => {
    const r = await change([{op: "add_scene", key: "n", title: `Scene ${S.scenes.length + 1}`}]);
    selectScene(r.new_ids.n); setTab("shot");
  };
  function selectScene(id) { sel.scene = id; sel.shot = sceneShots(id)[0]?.id || null; sel.clip = null; Player.stop(); paint(); }

  function paintRefs() {
    $("refs").innerHTML = S.refs.length ? S.refs.map((r) => `<div class="ed-ref" data-ref="${r.id}">
      <div class="th">${r.image ? `<img src="${esc(r.image)}" alt="" loading="lazy">` : r.image_pending ? "⏳" : ({character: "👤", location: "🏛", prop: "🗝", style: "🎨", voice: "🎙"}[r.kind] || "📌")}</div>
      <div><b>${esc(r.name)}</b><span>${esc(r.kind[0].toUpperCase() + r.kind.slice(1))}${r.description ? " · " + esc(r.description) : ""}</span>${r.voice_pending ? `<span class="v">🎙 designing voice…</span>` : r.voice_designed ? `<span class="v">🎙 own voice</span>` : r.voice ? `<span class="v">🎙 voice sample</span>` : r.speaker ? `<span class="v">🎙 ${esc(r.speaker)}</span>` : ""}${r.kind === "character" || r.kind === "location" ? `<span class="v" style="color:var(--muted)">🔄 ${(r.angles || []).filter((a) => a.status === "succeeded" && !a.angle.startsWith("photo_")).length}/4 ${r.kind === "location" ? "views" : "angles"}${(r.angles || []).some((a) => a.angle.startsWith("photo_")) ? ` + ${(r.angles || []).filter((a) => a.angle.startsWith("photo_")).length} photo(s)` : ""}${(r.angles || []).some((a) => !["succeeded", "failed", "gone"].includes(a.status)) || r.angles_waiting ? " · drawing" : ""}</span>` : ""}</div>
      <span>⋮</span></div>`).join("") : `<div class="ed-note" style="padding:4px 6px">Characters, places and voices the film keeps coming back to. Shots that include them get them as references, so faces and voices stay consistent.</div>`;
  }
  $("refs").addEventListener("click", (e) => { const d = e.target.closest("[data-ref]"); if (d) refModal(refById(d.dataset.ref)); });
  $("add-ref").onclick = () => refModal(null);
  if ($("refs-lib")) $("refs-lib").onclick = () => libraryModal("");
  if ($("refs-voices")) $("refs-voices").onclick = async () => {
    const presets = S.refs.filter((r) => (r.kind === "character" || r.kind === "voice") && r.speaker && !r.voice).length;
    const replace = presets ? confirm(`Design a voice for every character from their description.\n\n${presets} character(s) use a preset voice. Replace those with designed voices too? (OK = yes, Cancel = keep the presets)`) : false;
    try { const d = await api(`/api/editor/${PID}/design-voices`, {replace_presets: replace}); toast(d.characters ? `Designing ${d.characters} voice(s); their lines follow automatically` : "Every character already has a voice"); load(true); }
    catch (e) { toast(e.message); } };
  $("refs-gen").onclick = async () => {
    const missing = S.refs.filter((r) => !r.image && !r.image_pending).length;
    if (!missing) { toast("Every reference already has a picture"); return; }
    $("refs-gen").disabled = true; toast(`Drawing ${missing} reference picture(s)…`);
    try { await api(`/api/editor/${PID}/ref-pictures`, {}); load(true); } catch (e) { toast(e.message); }
    finally { $("refs-gen").disabled = false; }
  };
  $("open-left").onclick = () => ED.classList.toggle("left-open");

  const HINDI = new Set(["Meera", "Anaya", "Arjun", "Vikram"]), BURMESE = new Set(["Thiri", "Nandar", "Kyaw", "U_Ba"]);
  const voiceLabel = (s) => HINDI.has(s) ? `${s} — Hindi-native (other languages: her/his voice, cloned)` : BURMESE.has(s) ? `${s} — Burmese only` : s === "Vagdhenu" ? `${s} — Sanskrit chant` : s;
  // Each open of the reference dialog gets a number; timers and polls of an earlier dialog
  // stop as soon as another one opens (2026-11-25: with Nārada open, the views of a character
  // opened earlier kept being painted into it every 3 s, so another character's pictures
  // appeared in Nārada's dialog).
  let refGen = 0;
  function refModal(r) {
    const kinds = ["character", "location", "prop", "style", "voice"];
    let pickedImage = null, polling = null;
    const gen = ++refGen, live = () => gen === refGen && !$("modal").hidden;
    const voiced = (k) => k === "character" || k === "voice";
    modal(`<h2>${r ? "Edit reference" : "New reference"}</h2>
      <div class="ed-row2"><label class="ed-field">Name<input id="rf-name" value="${esc(r?.name || "")}" maxlength="80" placeholder="Name"></label>
      <label class="ed-field">Kind<select id="rf-kind">${kinds.map((k) => `<option ${r?.kind === k ? "selected" : ""}>${k}</option>`).join("")}</select></label></div>
      <label class="ed-field">Description (used in prompts)<textarea id="rf-desc" rows="3" placeholder="How they look — used in every prompt they appear in">${esc(r?.description || "")}</textarea></label>
      <div class="ed-field">Picture
        <div class="ed-refpic">
          <div class="ed-drop" id="rf-pic">${r?.image ? `<img src="${esc(r.image)}" alt="">` : r?.image_pending ? "⏳ being drawn…" : ""}Click, drop or paste (Ctrl/⌘+V) a picture</div>
          <div class="ed-refgen">
            <button class="ed-btn gold" type="button" id="rf-gen">✨ Generate picture</button>
            <button class="ed-btn sm" type="button" id="rf-paste" title="Use the picture you copied">📋 Paste picture</button>
            <button class="ed-btn sm" type="button" id="rf-lib" title="A character, place or voice from your other productions">⤓ From my characters</button>
            <label class="ed-note">Options <select id="rf-count"><option>1</option><option selected>2</option><option>3</option><option>4</option></select></label>
            <label class="ed-note">Model <select id="rf-model">${modelOptions("")}</select></label>
            <details><summary class="ed-note">Edit the prompt</summary><textarea id="rf-prompt" rows="3" placeholder="Empty = from the name, kind and description, in the film's style"></textarea></details>
            ${r?.image ? `<details class="rf-editbox"><summary class="ed-note">✏️ Edit this picture (change only part of it)</summary><textarea id="rf-edit" rows="2" placeholder="e.g. glasses off · grey hair · a red shawl instead of the blue one"></textarea><button class="ed-btn sm" type="button" id="rf-editgo">✏️ Make edited versions</button></details>` : ""}
          </div>
        </div>
        <div class="ed-refopts" id="rf-opts"></div></div>
      ${r && (r.kind === "character" || r.kind === "location") ? `<div class="ed-field">${r.kind === "location" ? "Views of the place" : "Every angle"} — drawn from the main picture and your photos <div class="ed-turn" id="rf-turn"></div>
        <div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap;align-items:center"><button class="ed-btn sm" type="button" id="rf-photos">⤒ Add photos</button><label class="ed-note ed-drawwith">Draw with <select id="rf-vmodel" title="If a view comes out wrong, pick another model and press ↻ on it">${modelOptions("")}</select></label><button class="ed-btn sm" type="button" id="rf-angles">↻ Redraw all</button><span class="ed-note" id="rf-turn-note"></span></div>
        <div class="ed-note" style="margin-top:4px">A view not right? Pick another model under “Draw with” and press ↻ on it — or use your own photos: ⤒ on a view replaces it, ✕ removes it, and “Add photos” takes several at once. Your photos are never redrawn.</div></div>` : ""}
      <input type="file" id="rf-picfile" accept="image/*" hidden>
      <div class="ed-field" id="rf-designrow" ${voiced(r?.kind || "character") ? "" : "hidden"}>Their voice — designed from this description, then used for every line they say
        <textarea id="rf-vdesign" rows="2" placeholder="Empty = written from the description above (gender, age, pitch, timbre, pace, accent)">${esc(r?.voice_design || "")}</textarea>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px">${r ? `<button class="ed-btn sm" type="button" id="rf-design">✨ ${r.voice_designed ? "Design a new voice" : "Design this voice"}</button>` : ""}<span class="ed-note" id="rf-vstate">${r?.voice_pending ? "🎙 designing their voice…" : r?.voice_designed ? "🎙 their designed voice is in use" : r?.voice ? "🎙 your voice sample is in use" : r?.speaker ? "🎙 a preset voice is in use" : (r ? "" : "Designed automatically when you save")}</span></div></div>
      <div class="ed-row2" id="rf-voicerow" ${voiced(r?.kind || "character") ? "" : "hidden"}><label class="ed-field">…or a preset voice<select id="rf-speaker"><option value="">— their own designed voice —</option>${S.speakers.map((s) => `<option value="${esc(s)}" ${r?.speaker === s ? "selected" : ""}>${esc(voiceLabel(s))}</option>`).join("")}</select></label>
      <label class="ed-field">…or a voice sample to clone<div class="ed-drop" id="rf-voice">${r?.voice ? "🎙 sample saved — click to replace" : "5–30 s of clear speech"}</div><input type="file" id="rf-voicefile" accept="audio/*" hidden></label></div>
      ${r?.voice ? `<audio src="${esc(r.voice)}" controls style="width:100%"></audio>` : ""}
      <div class="acts">${r ? `<button class="ed-btn danger" data-act="delete" style="margin-right:auto">Delete</button>` : ""}<button class="ed-btn ghost" data-act="close">Cancel</button><button class="ed-btn gold" data-act="save">Save</button></div>`,
    async (act) => {
      clearTimeout(polling);
      if (act === "delete") { if (!confirm(`Delete ${r.name}? Shots stop using it.`)) return false; await api(`/api/editor/${PID}/refs/${r.id}/delete`, {}); load(true); return true; }
      if (act !== "save") return true;
      try { await saveRef(); toast("Saved"); load(true); return true; } catch (e) { toast(e.message); return false; }
    });
    async function saveRef() {
      const fd = new FormData();
      fd.append("name", $("rf-name").value.trim()); fd.append("kind", $("rf-kind").value);
      fd.append("description", $("rf-desc").value);
      fd.append("speaker", voiced($("rf-kind").value) ? $("rf-speaker").value : "");
      if (voiced($("rf-kind").value)) fd.append("voice_design", $("rf-vdesign").value.trim());
      if ($("rf-picfile").files[0]) fd.append("picture", $("rf-picfile").files[0]);
      else if (pickedImage) fd.append("image_id", pickedImage);
      if (voiced($("rf-kind").value) && $("rf-voicefile").files[0]) fd.append("voice", $("rf-voicefile").files[0]);
      if (!fd.get("name")) throw new Error("Give it a name");
      const d = await api(`/api/editor/${PID}/refs${r ? "/" + r.id : ""}`, fd);
      if (!r && d.id) r = {id: d.id, kind: fd.get("kind"), name: fd.get("name")};
      return r;
    }
    $("rf-kind").onchange = () => { $("rf-voicerow").hidden = $("rf-designrow").hidden = !voiced($("rf-kind").value); };
    if ($("rf-design")) $("rf-design").onclick = async () => {
      const b = $("rf-design"); b.disabled = true;
      try { await saveRef(); const d = await api(`/api/editor/${PID}/refs/${r.id}/design-voice`, {voice: $("rf-vdesign").value.trim(), redo: true});
        $("rf-vdesign").value = d.voice || $("rf-vdesign").value; $("rf-vstate").textContent = "🎙 designing their voice… their lines follow it automatically"; toast("Designing the voice"); load(true); }
      catch (e) { toast(e.message); } finally { b.disabled = false; }
    };
    const ANG = r && r.kind === "location" ? {wide: "Wide", reverse: "Reverse", detail: "Detail", aerial: "Aerial"} : {three_quarter: "¾ view", side: "Side", back: "Back", face: "Face"};
    function paintTurn() {
      if (!live() || !$("rf-turn") || !r) return;
      const cur = S.refs.find((x) => x.id === r.id) || r;
      const by = Object.fromEntries((cur.angles || []).map((a) => [a.angle, a]));
      const mine = new Set(cur.mine || []), skipped = new Set(cur.skipped || []);
      const tools = (k, canRedraw) => `<span class="tt">${canRedraw ? `<button type="button" data-angle="${k}" title="Draw it again">↻</button>` : ""}<button type="button" data-vup="${k}" title="Use my photo for this">⤒</button>${by[k] ? `<button type="button" data-vdel="${k}" title="Remove">✕</button>` : ""}</span>`;
      $("rf-turn").innerHTML = `<div class="t main">${cur.image ? `<img src="${esc(cur.image)}" alt="">` : "—"}<span>${cur.kind === "location" ? "Main" : "Front"}</span></div>`
        + Object.keys(ANG).map((k) => { const a = by[k];
          return `<div class="t ${mine.has(k) ? "mine" : ""}">${a && a.status === "succeeded" ? `<img src="${esc(a.url)}" alt="">` : a && a.status !== "failed" ? `<i class="spin"></i>` : skipped.has(k) ? "removed" : cur.angles_waiting ? "⏳" : "—"}<span>${ANG[k]}${mine.has(k) ? " · yours" : ""}</span>${tools(k, true)}</div>`; }).join("")
        + (cur.angles || []).filter((a) => a.angle.startsWith("photo_")).map((a) => `<div class="t mine">${a.status === "succeeded" ? `<img src="${esc(a.url)}" alt="">` : `<i class="spin"></i>`}<span>Photo ${a.angle.slice(6)}</span>${tools(a.angle, false)}</div>`).join("");
      const pending = (cur.angles || []).filter((a) => !["succeeded", "failed", "cancelled", "gone"].includes(a.status)).length;
      $("rf-turn-note").textContent = !cur.image ? "Set the main picture first; the angles follow automatically." : cur.angles_waiting ? "Waiting for the main picture to finish…" : pending ? `Drawing ${pending} angle(s)…` : "";
    }
    paintTurn();
    const turnTimer = setInterval(async () => { if (!live() || !$("rf-turn")) { clearInterval(turnTimer); return; } await load(); paintTurn(); }, 3000);
    const vmodel = () => ($("rf-vmodel") && $("rf-vmodel").value) || null;
    if ($("rf-angles")) $("rf-angles").onclick = async () => { try { await api(`/api/editor/${PID}/refs/${r.id}/angles`, {model: vmodel()}); toast("Drawing every view again (your own photos are kept)"); await load(true); paintTurn(); } catch (e) { toast(e.message); } };
    const pickPhotos = (multiple, slot) => { const inp = document.createElement("input"); inp.type = "file"; inp.accept = "image/*"; inp.multiple = multiple;
      inp.onchange = async () => { if (!inp.files.length) return; const fd = new FormData(); [...inp.files].forEach((f) => fd.append("file", f)); if (slot) fd.append("slot", slot);
        try { const d = await api(`/api/editor/${PID}/refs/${r.id}/photos`, fd); toast(`Added ${d.placed.length} photo(s)` + (d.dropped ? ` · ${d.dropped} over the limit of 6 extra photos` : "")); await load(true); paintTurn(); } catch (err) { toast(err.message); } };
      inp.click(); };
    if ($("rf-photos")) $("rf-photos").onclick = () => { if (!r) { toast("Save it first"); return; } pickPhotos(true, null); };
    if ($("rf-turn")) $("rf-turn").addEventListener("click", async (e) => {
      const up = e.target.closest("[data-vup]"); if (up) { pickPhotos(false, up.dataset.vup); return; }
      const del = e.target.closest("[data-vdel]"); if (del) { try { await api(`/api/editor/${PID}/refs/${r.id}/views/${del.dataset.vdel}/delete`, {}); toast("Removed"); await load(true); paintTurn(); } catch (err) { toast(err.message); } return; }
      const b = e.target.closest("[data-angle]"); if (!b) return;
      try { await api(`/api/editor/${PID}/refs/${r.id}/angles`, {only: [b.dataset.angle], model: vmodel()}); toast("Redrawing that view" + (vmodel() ? " with " + IMG_MODELS.find((m) => m[0] === vmodel())[1].split(" —")[0] : "")); await load(true); paintTurn(); } catch (err) { toast(err.message); } });
    $("rf-gen").onclick = async () => {
      const btn = $("rf-gen"); btn.disabled = true;
      try {
        await saveRef();                         // a new reference is created first; edits are kept
        const d = await api(`/api/editor/${PID}/refs/${r.id}/generate`, {prompt: $("rf-prompt").value, count: +$("rf-count").value, model: $("rf-model").value || null});
        if (!$("rf-prompt").value) $("rf-prompt").placeholder = d.prompt;
        const ids = d.image_ids;
        $("rf-opts").innerHTML = ids.map((id, i) => `<button type="button" class="opt" data-img="${id}"><span>drawing ${i + 1}…</span><span class="pg"><i></i></span></button>`).join("");
        const t0 = Date.now();
        const poll = async () => {
          if (!live() || !$("rf-opts")) return;
          const st = await api(`/api/editor/${PID}/images?ids=${ids.join(",")}`);
          for (const im of st.images) {
            const el = $("rf-opts").querySelector(`[data-img="${im.image_id}"]`); if (!el) continue;
            if (im.status === "succeeded" && !el.querySelector("img")) el.innerHTML = `<img src="${esc(im.url)}" alt="option">`;
            else if (im.status === "failed") el.innerHTML = "failed";
          }
          const left = st.images.filter((x) => !["succeeded", "failed", "cancelled"].includes(x.status)).length;
          btn.textContent = left ? `✨ Drawing… ${Math.round((Date.now() - t0) / 1000)} s` : "✨ Generate again";
          if (left) polling = setTimeout(poll, 2000); else { btn.disabled = false; toast("Click a picture to use it"); }
        };
        poll();
      } catch (e) { toast(e.message); btn.disabled = false; }
    };
    if ($("rf-editgo")) $("rf-editgo").onclick = async () => {
      const ins = $("rf-edit").value.trim(); if (!ins) { toast("Say what should change"); return; }
      const btn = $("rf-editgo"); btn.disabled = true;
      try {
        const d = await api(`/api/editor/${PID}/refs/${r.id}/edit`, {instruction: ins, count: +$("rf-count").value});
        const ids = d.image_ids;
        $("rf-opts").innerHTML = ids.map((id, i) => `<button type="button" class="opt" data-img="${id}"><span>editing ${i + 1}…</span><span class="pg"><i></i></span></button>`).join("");
        const t0 = Date.now();
        const poll = async () => {
          if (!live() || !$("rf-opts")) return;
          const st = await api(`/api/editor/${PID}/images?ids=${ids.join(",")}`);
          for (const im of st.images) {
            const el = $("rf-opts").querySelector(`[data-img="${im.image_id}"]`); if (!el) continue;
            if (im.status === "succeeded" && !el.querySelector("img")) el.innerHTML = `<img src="${esc(im.url)}" alt="edited">`;
            else if (im.status === "failed") el.innerHTML = "failed";
          }
          const left = st.images.filter((x) => !["succeeded", "failed", "cancelled"].includes(x.status)).length;
          btn.textContent = left ? `✏️ Editing… ${Math.round((Date.now() - t0) / 1000)} s` : "✏️ Make edited versions";
          if (left) polling = setTimeout(poll, 2000); else { btn.disabled = false; toast("Click a version to use it, then Save"); }
        };
        poll();
      } catch (e) { toast(e.message); btn.disabled = false; }
    };
    $("rf-opts").addEventListener("click", async (e) => {
      const b = e.target.closest("[data-img]"); if (!b || !b.querySelector("img")) return;
      pickedImage = b.dataset.img; $("rf-picfile").value = "";
      $("rf-opts").querySelectorAll(".opt").forEach((x) => x.classList.toggle("on", x === b));
      $("rf-pic").innerHTML = `<img src="${esc(b.querySelector("img").src)}" alt="">Chosen — Save to keep it`;
    });
    const hook = (drop, input, show) => { $(drop).onclick = () => $(input).click(); $(input).onchange = () => show($(input).files[0]);
      $(drop).ondragover = (e) => e.preventDefault(); $(drop).ondrop = (e) => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (!f) return; const dt = new DataTransfer(); dt.items.add(f); $(input).files = dt.files; show(f); }; };
    const showPic = (f) => { pickedImage = null; $("rf-pic").innerHTML = `<img src="${URL.createObjectURL(f)}" alt="">${esc(f.name || "pasted picture")} — Save to keep it`; };
    hook("rf-pic", "rf-picfile", showPic);
    hook("rf-voice", "rf-voicefile", (f) => { $("rf-voice").textContent = "🎙 " + f.name; });
    // Paste a picture straight in (the person, 2026-11-25: "simply paste the character
    // references instead of only drop or select"). Text still pastes into the fields.
    const usePicture = (f) => { if (!f) return; const dt = new DataTransfer(); dt.items.add(new File([f], f.name || "pasted.png", {type: f.type || "image/png"})); $("rf-picfile").files = dt.files; showPic($("rf-picfile").files[0]); toast("Picture pasted — Save to use it"); };
    const onPaste = (e) => {
      if (!live()) { document.removeEventListener("paste", onPaste); return; }
      const f = [...(e.clipboardData?.items || [])].find((i) => i.kind === "file" && i.type.startsWith("image/"));
      if (!f) return;
      e.preventDefault(); usePicture(f.getAsFile());
    };
    document.addEventListener("paste", onPaste);
    $("rf-paste").onclick = async () => {
      try {
        for (const item of await navigator.clipboard.read()) {
          const t = item.types.find((x) => x.startsWith("image/"));
          if (t) { usePicture(await item.getType(t)); return; }
        }
        toast("No picture on the clipboard — copy one first");
      } catch { toast("Press Ctrl+V (⌘V on a Mac) to paste the picture"); }
    };
    $("rf-lib").onclick = () => libraryModal(r ? r.name : $("rf-name").value.trim());
  }

  // Characters from the person's other productions (2026-11-25: "allow users see all their
  // previous characters in the character reference and add them here for consistency").
  async function libraryModal(query) {
    refGen++;                                     // the reference dialog underneath is closed
    modal(`<h2>My characters</h2>
      <p class="ed-note">Characters, places and voices from your other productions. Pick any to add them here with their pictures, views and voice; a name that is already here is filled in, not doubled.</p>
      <input id="lib-q" placeholder="Search by name or description" value="${esc(query || "")}" style="width:100%">
      <div class="ed-lib" id="lib-list"><div class="ed-note">Loading…</div></div>
      <div class="acts"><button class="ed-btn ghost" data-act="close">Close</button><button class="ed-btn gold" data-act="add" id="lib-add" disabled>Add</button></div>`,
      async (act) => {
        if (act !== "add") return true;
        const ids = [...document.querySelectorAll("#lib-list .on")].map((x) => x.dataset.lib);
        if (!ids.length) return false;
        try { const d = await api(`/api/editor/${PID}/refs/import`, {ref_ids: ids}); toast(`Added ${d.ids.length} to this production`); await load(true); return true; }
        catch (e) { toast(e.message); return false; }
      });
    const paint = async () => {
      let items = [];
      try { items = (await api(`/api/editor/${PID}/library?q=${encodeURIComponent($("lib-q").value.trim())}`)).items; } catch (e) { toast(e.message); }
      $("lib-list").innerHTML = items.length ? items.map((x) => `<button type="button" class="ed-libitem" data-lib="${x.ref_id}">
          <span class="th">${x.image ? `<img src="${esc(x.image)}" alt="" loading="lazy">` : ({character: "👤", location: "🏛", prop: "🗝", style: "🎨", voice: "🎙"}[x.kind] || "📌")}</span>
          <span class="tx"><b>${esc(x.name)}</b>${x.here ? ` <i>· already here (fills it in)</i>` : ""}<small>${esc(x.production)} · ${esc(x.kind)}${x.voice ? " · 🎙" : ""}</small><small>${esc((x.description || "").slice(0, 140))}</small></span></button>`).join("")
        : `<div class="ed-note">${$("lib-q").value.trim() ? "Nothing matches." : "No characters in your other productions yet."}</div>`;
    };
    let t = null; $("lib-q").oninput = () => { clearTimeout(t); t = setTimeout(paint, 250); };
    $("lib-list").onclick = (e) => { const b = e.target.closest("[data-lib]"); if (!b) return; b.classList.toggle("on"); const n = document.querySelectorAll("#lib-list .on").length; $("lib-add").disabled = !n; $("lib-add").textContent = n ? `Add ${n}` : "Add"; };
    paint();
  }

  // ------------------------------------------------------------------ center: crumbs, stage, strip
  function paintCrumbs() {
    const sc = sceneById(sel.scene), sh = shotById(sel.shot);
    $("crumb-scene").textContent = sc ? sc.title || "Untitled scene" : "";
    $("crumb-shot").textContent = sh ? `Shot ${String(sceneShots(sel.scene).indexOf(sh) + 1).padStart(2, "0")}` + (sh.title ? ` · ${sh.title}` : "") : "No shot yet";
  }
  $("prev").onclick = () => stepShot(-1); $("next").onclick = () => stepShot(1);
  function stepShot(d) { const all = S.shots; const i = all.findIndex((s) => s.id === sel.shot); const n = all[i + d]; if (n) { selectShot(n.id); } }
  function selectShot(id) { const sh = shotById(id); if (!sh) return; if (!preview || preview.shot !== id) preview = null; sel.shot = id; sel.scene = sh.scene_id; sel.clip = null; genOpen = null; Player.stop(); Player.t = sh.start; paint(); }

  function paintStage(at) {
    const sh = at || shotById(sel.shot);
    const st = $("stage");
    if (!sh) { $("frametools").hidden = true; st.innerHTML = `<div class="empty"><b>This scene has no shots yet</b>Add one below, or ask the agent to break the scene into shots.<br><br><button class="ed-btn gold" id="stage-add">＋ Add a shot</button> <button class="ed-btn" id="stage-ask">✨ Ask the agent</button></div>`;
      $("stage-add").onclick = addShot; $("stage-ask").onclick = () => { setTab("agent"); scope = "scene"; paintScope(); chatSend("Define the shots this scene should have."); }; return; }
    if (preview && preview.shot === sh.id) {
      st.innerHTML = `<div class="ed-frame" id="frame"><img src="${esc(preview.url)}" alt="picture option"></div><span class="ed-badge">✦ Option ${preview.n} of ${preview.of} — preview</span>
        <div class="use"><button class="ed-btn" id="pv-back">Back to the shot</button><button class="ed-btn gold" id="pv-use">Use this picture</button></div>`;
      fitFrame();
      $("pv-back").onclick = () => { preview = null; paintStage(); };
      $("pv-use").onclick = async () => { const p = preview; preview = null;
        try { await api(`/api/editor/${PID}/proposals/${p.prop}/apply`, {option: p.image_id}); toast("Picture set for this shot"); load(true); } catch (e) { toast(e.message); } };
      return;
    }
    const making = S.proposals.find((p) => p.shot_id === sh.id && p.options.some((o) => !["succeeded", "failed", "gone"].includes(o.status)));
    const busy = ["running", "pending"].includes(sh.status) && ["running", "queued"].includes(S.status);
    const mk = `<span class="ed-badge">${ICON[sh.make]} ${MAKE[sh.make][0]}${sh.framing ? " · " + sh.framing : ""} · ${sh.seconds}s</span>`;
    const state = sh.status === "done" ? "clip ready" : sh.status === "stale" ? "changed since rendered" : sh.status === "failed" ? "failed" : sh.clip ? "" : "not rendered";
    if (sh.clip && Player.mode !== "stage-img") {
      const snd = soundUnder(sh);
      st.innerHTML = `<div class="ed-frame" id="frame"><video id="stage-v" src="${esc(sh.clip)}" playsinline preload="auto" muted></video></div>${mk}<span class="ed-badge r">${esc(state)}</span>`
        + (Player.playing ? "" : `<button class="ed-bigplay" id="bigplay" type="button" aria-label="Play with sound">▶</button>`)
        + (snd.tracks.length ? `<span class="ed-sound">🔊 ${esc(snd.tracks.join(" · "))}</span>` : `<span class="ed-sound none">🔇 No sound in this shot <button type="button" id="give-sound">Give it sound</button></span>`);
      fitFrame();
      if ($("bigplay")) $("bigplay").onclick = () => { if ($("prange").value === "film") $("prange").value = "shot"; Player.play(); };
      if ($("give-sound")) $("give-sound").onclick = () => { setTab("shot"); $("pane-shot").scrollTop = 0; document.querySelector(".ed-silent")?.scrollIntoView({block: "start"}); };
    } else if (sh.picture) {
      const snd = soundUnder(sh);
      st.innerHTML = `<div class="ed-frame" id="frame"><img id="stage-i" src="${esc(sh.picture)}" alt="" class="${Player.playing ? "kb" : ""}"></div>${mk}<span class="ed-badge r">${esc(state)}</span><button class="ed-btn sm ed-stage-edit" id="stage-edit" type="button" title="Change something in this picture and keep the rest">✏️ Edit picture</button>`
        + (snd.tracks.length || !["animate", "motion"].includes(sh.make) ? (snd.tracks.length ? `<span class="ed-sound">🔊 ${esc(snd.tracks.join(" · "))}</span>` : "")
           : `<span class="ed-sound none">🔇 No sound in this shot <button type="button" id="give-sound">Give it sound</button></span>`);
      fitFrame();
      if ($("give-sound")) $("give-sound").onclick = () => { setTab("shot"); $("pane-shot").scrollTop = 0; };
      $("stage-edit").onclick = () => { setTab("shot"); genOpen = "edit"; paintInspector(); $("pane-shot").scrollTop = 0; setTimeout(() => $("g-edit")?.focus(), 50); };
    } else if (sh.start_from === "prev_last") {
      const prev = S.shots[S.shots.findIndex((x) => x.id === sh.id) - 1];
      st.innerHTML = `<div class="empty"><b>⛓ Opens on the last frame of the previous shot</b><span>Taken from “${esc(prev?.title || "the previous shot")}” when this shot renders, so the two flow as one take.</span>
        <div class="row"><button class="ed-btn gold" id="stage-chain-render">▶ Render both</button></div></div>${mk}`;
      $("stage-chain-render").onclick = () => renderShots([prev?.id, sh.id].filter(Boolean), "both shots");
    } else if (making) {
      const n = making.options.length, done = making.options.filter((o) => o.status === "succeeded").length;
      st.innerHTML = `<div class="gen"><span class="spin"></span><b>Making ${n} picture option${n === 1 ? "" : "s"}…</b><span class="ed-note">${done} of ${n} ready · ${Math.max(0, Math.round(S.now - making.created_at))} s · they appear in the panel on the right</span></div>${mk}`;
    } else if (!sh.needs_picture) {
      const why = {recast: "The action comes from the video: its frame is redrawn with your characters when the video is made (or now, with 🖼).", restyle: "The action comes from the video: its frame is redrawn with the change when the video is made (or now, with 🖼).", motion: "The moves come from the video and the character's own picture says who performs.", perform: "The video is made from the characters' and places' pictures (and reference videos)."}[sh.make] || "";
      st.innerHTML = `<div class="empty"><b>No starting picture needed</b><span>${why} You can still add one.</span>
        <div class="row"><button class="ed-btn" id="stage-gen">🖼 Make a picture anyway</button><button class="ed-btn" id="stage-up">⤒ Upload</button></div></div>${mk}`;
      $("stage-gen").onclick = () => { setTab("shot"); genOpen = "image"; paintInspector(); $("pane-shot").scrollTop = 0; };
      $("stage-up").onclick = () => uploadPicture(sh.id);
    } else {
      st.innerHTML = `<div class="empty"><b>No picture for this shot yet</b><span>Make options from its description and references, upload your own, or ask the agent.</span>
        <div class="row"><button class="ed-btn gold" id="stage-gen">🖼 Generate picture options</button><button class="ed-btn" id="stage-up">⤒ Upload</button></div></div>${mk}`;
      $("stage-gen").onclick = () => { setTab("shot"); genOpen = "image"; paintInspector(); $("pane-shot").scrollTop = 0; setTimeout(() => $("g-prompt")?.focus(), 50); };
      $("stage-up").onclick = () => uploadPicture(sh.id);
    }
    $("frametools").hidden = !(sh.clip && !preview);
    if (busy) st.insertAdjacentHTML("beforeend", `<div class="busy">⏳ ${esc(S.message || "rendering")}</div>`);
    if (sh.status === "failed" && sh.error) st.insertAdjacentHTML("beforeend", `<span class="ed-badge" style="top:auto;bottom:10px;color:var(--bad);max-width:90%">${esc(sh.error)}</span>`);
  }
  const RATIO = {"9:16": 9 / 16, "16:9": 16 / 9, "1:1": 1, "2.39:1": 2.39};
  function fitFrame() {
    const st = $("stage"), f = $("frame"); if (!f) return;
    const r = RATIO[S.aspect] || 9 / 16, W = st.clientWidth, H = st.clientHeight;
    const w = Math.min(W, H * r), h = w / r;
    f.style.width = w + "px"; f.style.height = h + "px";
  }
  addEventListener("resize", () => S && fitFrame());
  if (window.ResizeObserver) new ResizeObserver(() => S && fitFrame()).observe($("stage"));
  $("aspect").onchange = async () => {
    const v = $("aspect").value;
    try { const r = await api(`/api/editor/${PID}/settings`, {aspect: v});
      toast(r.rerender ? `Now ${v}. Rendered clips were made for the old shape and are marked to render again.` : `Now ${v}` + (v === "2.39:1" ? " — rendered 16:9, cropped to scope on export" : ""));
      load(true); } catch (e) { toast(e.message); }
  };
  const curVideoTime = () => { const v = $("stage-v"); return v ? v.currentTime : 0; };
  async function useFrame(at, to) {
    const sh = shotById(sel.shot); if (!sh) return;
    try {
      const r = await api(`/api/editor/${PID}/shots/${sh.id}/frame`, {at, to});
      toast(at === "last" ? "Last frame is now the next shot's picture" : `Frame at ${(+at).toFixed(2)} s used`);
      await load(true); if (to !== "this") selectShot(r.shot_id);
    } catch (e) { toast(e.message); }
  }
  $("fr-last").onclick = () => useFrame("last", "next");
  $("fr-pick").onclick = () => {
    const v = $("stage-v"); if (v) v.pause();
    const t = curVideoTime(), sh = shotById(sel.shot);
    const next = S.shots[S.shots.findIndex((x) => x.id === sh.id) + 1];
    modal(`<h2>Use a frame of “${esc(sh.title || "this shot")}”</h2>
      <div class="ed-seg" id="fr-which"><button type="button" class="on" data-w="now">The frame showing now · ${t.toFixed(2)} s</button><button type="button" data-w="last">The last frame</button></div>
      <div class="ed-note">Scrub the video under the stage to pick a moment; the frame is taken from the rendered clip at full size.</div>
      <div class="acts" style="justify-content:stretch;display:grid;grid-template-columns:1fr 1fr 1fr">
        <button class="ed-btn ${next ? "gold" : ""}" data-act="next" ${next ? "" : "disabled"} title="${next ? esc(next.title || "next shot") : "there is no next shot"}">As the next shot's picture</button>
        <button class="ed-btn ${next ? "" : "gold"}" data-act="new">As a new shot after this</button>
        <button class="ed-btn" data-act="this">As this shot's picture</button></div>
      <div class="acts"><button class="ed-btn ghost" data-act="close">Cancel</button></div>`, async (act) => {
      if (act === "close") return true;
      const which = document.querySelector("#fr-which .on")?.dataset.w;
      await useFrame(which === "last" ? "last" : t, act); return true;
    });
    document.querySelectorAll("#fr-which button").forEach((b) => b.onclick = () => document.querySelectorAll("#fr-which button").forEach((x) => x.classList.toggle("on", x === b)));
  };
  function soundUnder(sh) {
    const on = S.clips.filter((c) => c.status !== "failed" && c.start_s < sh.start + sh.seconds && c.start_s + (c.duration_s || 3) > sh.start && !S.mix[c.track].mute);
    const tracks = [...new Set(on.map((c) => TRACK_NAME[c.track]))];
    if (sh.make === "perform" && sh.status === "done" && !S.mix.shot.mute) tracks.push("its own sound");
    return {tracks, lines: on.filter((c) => c.track === "dialogue")};
  }
  function uploadPicture(shotId) {
    const inp = document.createElement("input"); inp.type = "file"; inp.accept = "image/*";
    inp.onchange = async () => { const fd = new FormData(); fd.append("file", inp.files[0]);
      try { await api(`/api/editor/${PID}/shots/${shotId}/picture`, fd); toast("Picture set"); load(true); } catch (e) { toast(e.message); } };
    inp.click();
  }

  function paintStrip() {
    const shots = sceneShots(sel.scene);
    $("strip").innerHTML = shots.map((s, i) => `<div class="ed-card ${s.id === sel.shot ? "on" : ""}" data-shot="${s.id}" draggable="true" title="Drag to reorder, or onto a scene on the left to move it there">
      ${s.start_from === "prev_last" ? `<span class="chain" title="Opens on the previous shot's last frame">⛓ continues</span>` : ""}
      <div class="th">${s.picture ? `<img src="${esc(s.picture)}" alt="" loading="lazy">` : s.start_from === "prev_last" ? "" : "no picture"}</div>
      <span class="mk" title="${MAKE[s.make][0]}">${ICON[s.make]} ${s.seconds}s</span>
      ${(() => { const b = s.auto ? {continuity: "🔗 checking continuity", refs: "⏳ characters & places", picture: "🖼 making picture", render: s.start_from === "prev_last" ? "⛓ after previous" : "⏳ to render"}[s.auto] : {done: "● clip", running: "⏳ rendering", failed: "failed", stale: "changed"}[s.status] || (s.status === "pending" && ["running", "queued"].includes(S.status) ? "queued" : ""); return b ? `<span class="st ${s.auto ? "running" : esc(s.status)}">${b}</span>` : ""; })()}
      <div class="m"><b>Shot ${String(i + 1).padStart(2, "0")}</b><span class="f">${esc(s.framing)}</span><span>${esc(s.title || s.prompt || "untitled")}</span></div></div>`).join("")
      + `<button class="ed-card add" id="add-shot" type="button" title="Add a shot" aria-label="Add a shot">＋</button>`;
    $("add-shot").onclick = addShot;
  }
  $("strip").addEventListener("click", (e) => { const d = e.target.closest("[data-shot]"); if (d) selectShot(d.dataset.shot); });
  let draggingShot = null;
  $("strip").addEventListener("dragstart", (e) => { const d = e.target.closest("[data-shot]"); if (!d) return; draggingShot = d.dataset.shot; d.classList.add("dragging"); e.dataTransfer.setData("text/plain", draggingShot); });
  $("strip").addEventListener("dragend", () => { draggingShot = null; document.querySelectorAll(".dragging, .drop-into").forEach((x) => x.classList.remove("dragging", "drop-into")); });
  $("scenes").addEventListener("dragover", (e) => { if (!draggingShot) return; const d = e.target.closest("[data-scene]"); if (!d) return; e.preventDefault(); e.stopPropagation();
    document.querySelectorAll(".drop-into").forEach((x) => x.classList.remove("drop-into")); d.classList.add("drop-into"); });
  $("scenes").addEventListener("drop", (e) => { if (!draggingShot) return; const d = e.target.closest("[data-scene]"); if (!d) return; e.preventDefault(); e.stopImmediatePropagation();
    const id = draggingShot, sc = sceneById(d.dataset.scene); draggingShot = null; document.querySelectorAll(".drop-into").forEach((x) => x.classList.remove("drop-into"));
    moveShot(id, sc.id, sc.shots.filter((x) => x !== id).length, `Moved to “${sc.title}”`); }, true);
  dragList($("strip"), "[data-shot]", (id, toIndex) => change([{op: "edit_shot", shot_id: id, scene_id: sel.scene, position: toIndex}], "Shot moved"));
  async function moveShot(id, sceneId, position, msg) {
    try { await change([{op: "edit_shot", shot_id: id, scene_id: sceneId, position}], msg || "Shot moved"); selectShot(id); }
    catch { /* change() already toasted the reason (e.g. rendering) */ }
  }
  function nudge(id, d) {
    const sh = shotById(id); if (!sh) return;
    const inScene = sceneShots(sh.scene_id); const i = inScene.findIndex((s) => s.id === id);
    if (i + d >= 0 && i + d < inScene.length) return moveShot(id, sh.scene_id, i + d, d < 0 ? "Moved earlier" : "Moved later");
    // past the scene's edge: into the neighbouring scene
    const si = S.scenes.findIndex((s) => s.id === sh.scene_id), ns = S.scenes[si + d];
    if (!ns) { toast(d < 0 ? "Already the first shot" : "Already the last shot"); return; }
    return moveShot(id, ns.id, d < 0 ? ns.shots.length : 0, `Moved to “${ns.title}”`);
  }
  async function addShot() {
    if (!sel.scene) return;
    const after = sceneShots(sel.scene).findIndex((s) => s.id === sel.shot);
    const cur = shotById(sel.shot);
    const r = await change([{op: "add_shot", key: "n", scene_id: sel.scene, title: "", seconds: 5, position: after >= 0 ? after + 1 : null, refs: cur ? cur.refs : []}]);
    selectShot(r.new_ids.n); setTab("shot");
  }

  // generic drag-to-reorder for scenes and shot cards
  function dragList(container, selector, onDrop) {
    let dragId = null;
    container.addEventListener("dragstart", (e) => { const d = e.target.closest(selector); if (!d) return; dragId = Object.values(d.dataset)[0]; e.dataTransfer.effectAllowed = "move"; });
    container.addEventListener("dragover", (e) => { const d = e.target.closest(selector); if (!d || !dragId) return; e.preventDefault(); container.querySelectorAll(".drop").forEach((x) => x.classList.remove("drop")); d.classList.add("drop"); });
    container.addEventListener("drop", (e) => {
      e.preventDefault(); const d = e.target.closest(selector); container.querySelectorAll(".drop").forEach((x) => x.classList.remove("drop"));
      if (!d || !dragId) return; const items = [...container.querySelectorAll(selector)].map((x) => Object.values(x.dataset)[0]);
      const to = items.indexOf(Object.values(d.dataset)[0]); const from = items.indexOf(dragId);
      if (to !== from && to >= 0) onDrop(dragId, to > from ? to : to);
      dragId = null;
    });
    container.addEventListener("dragend", () => { dragId = null; container.querySelectorAll(".drop").forEach((x) => x.classList.remove("drop")); });
  }

  // ------------------------------------------------------------------ timeline
  const TL = {a: 0, b: 10, pps: 40, off: 150, zoom: 1};
  function tlRange() {
    if (view === "timeline" || !sel.scene) return [0, Math.max(10, S.duration)];
    const [a, b] = sceneSpan(sel.scene);
    return b > a ? [a, b] : [a, a + 10];
  }
  function paintTimeline() {
    const [a, b] = tlRange(); TL.a = a; TL.b = b;
    const lanesEl = $("lanes");
    TL.off = innerWidth <= 780 ? 84 : 150;            // the lane-label column (editor.css)
    const fit = Math.max(200, $("tl-scroll").clientWidth - TL.off - 4);
    const w = Math.round(fit * TL.zoom);
    lanesEl.style.setProperty("--lanew", w + "px");
    TL.pps = w / Math.max(1, b - a);
    const x = (t) => (t - a) * TL.pps;
    const unvoiced = S.clips.filter((c) => c.status === "script" && !c.auto).length;
    const noLines = S.shots.length && !S.clips.some((c) => c.track === "dialogue");
    $("tl-time").innerHTML = `${fmt(a)} – ${fmt(b)}` + (unvoiced ? ` <button class="ed-btn sm gold" id="voice-all" type="button" title="Voice all ${unvoiced} line(s)">🎙 Voice ${unvoiced}</button>`
      : noLines && S.story ? ` <button class="ed-btn sm gold" id="add-lines" type="button" title="The agent puts the story's dialogue and narration under the shots, then they are voiced">📝 Add lines</button>` : "");
    if ($("add-lines")) $("add-lines").onclick = addLines;
    if ($("voice-all")) $("voice-all").onclick = async () => { const d = await api(`/api/editor/${PID}/voice-all`, {}); toast(`Voicing ${d.queued} line(s) in the characters' voices`); load(true); };
    const step = [0.5, 1, 2, 5, 10, 15, 30, 60, 120].find((s) => s * TL.pps >= 46) || 300;
    let ticks = ""; for (let t = Math.ceil(a / step) * step; t <= b; t += step) ticks += `<span class="ed-tick" style="left:${x(t)}px">${fmt(t).replace(/\.0$/, "")}</span>`;
    const shotsIn = S.shots.filter((s) => s.start + s.seconds > a && s.start < b);
    const mix = S.mix;
    const lh = (track) => {
      const m = mix[track];
      return `<div class="ed-lh"><span class="nm"><i style="background:${TRACK_COLOR[track]}"></i>${TRACK_NAME[track]}</span>
        <input type="range" min="-30" max="6" step="1" value="${m.gain_db}" data-gain="${track}" title="${TRACK_NAME[track]} level ${m.gain_db} dB" aria-label="${TRACK_NAME[track]} level">
        <button data-mute="${track}" class="${m.mute ? "on" : ""}" title="${m.mute ? "Unmute" : "Mute"}" aria-label="Mute ${TRACK_NAME[track]}">${m.mute ? "🔇" : "🔈"}</button></div>`;
    };
    const blocks = (track) => S.clips.filter((c) => c.track === track && c.start_s < b && c.start_s + (c.duration_s || 3) > a).map((c) => {
      const d = c.duration_s || (c.status === "pending" ? 4 : 1);
      return `<div class="ed-blk ${track} ${c.status} ${c.id === sel.clip ? "sel" : ""}" data-clip="${c.id}" style="left:${x(c.start_s)}px;width:${Math.max(8, d * TL.pps)}px" title="${c.status === "script" ? "Script line — not voiced yet" + (c.auto ? " (being voiced)" : "") + ": " : ""}${esc(c.name)}${c.error ? " — " + esc(c.error) : ""}">
        <span>${c.status === "pending" ? "⏳ " : c.status === "script" ? (c.auto ? "⏳📝 " : "📝 ") : ""}${esc(c.name)}</span>${c.use_in_video ? `<span class="vid">🎬</span>` : ""}<span class="rs"></span></div>`;
    }).join("");
    const shotSound = shotsIn.filter((s) => ["perform", "lipsync"].includes(s.make)).map((s) => `<div class="ed-blk shot ${s.id === sel.shot ? "on" : ""}" style="left:${x(s.start)}px;width:${s.seconds * TL.pps}px;opacity:.75" title="${s.make === "lipsync" ? "Lip-sync: the line is on the dialogue track, so this isn't mixed twice" : "The sound the reference video model made (muted unless you turn it on)"}">${s.make === "lipsync" ? "👄 in dialogue" : "🧩 " + esc(s.title || "shot")}</div>`).join("");
    const keep = [$("tl-scroll").scrollLeft, $("tl-scroll").scrollTop];
    lanesEl.innerHTML = `<div></div><div class="ed-lane ruler" id="ruler">${ticks}</div>
      <div class="ed-lh"><span class="nm">🎞 Shots</span></div><div class="ed-lane shots">${shotsIn.map((s) => `<div class="ed-blk shot ${s.id === sel.shot ? "on" : ""} ${s.scene_id !== sel.scene && view !== "timeline" ? "other" : ""}" data-tshot="${s.id}" title="Drag to move this shot" style="cursor:grab;left:${x(s.start)}px;width:${s.seconds * TL.pps - 2}px">${esc(s.framing || "")} ${esc(s.title || "shot " + s.n)}</div>`).join("")}</div>
      ${["dialogue", "ambience", "music"].map((t) => lh(t) + `<div class="ed-lane" data-lane="${t}">${blocks(t)}</div>`).join("")}
      ${S.shots.some((s) => ["perform", "lipsync"].includes(s.make)) ? lh("shot") + `<div class="ed-lane" style="cursor:default">${shotSound}</div>` : ""}
      <div class="ed-head" id="head" style="left:${TL.off + x(Player.t)}px"></div>`;
    $("tl-scroll").scrollLeft = keep[0]; $("tl-scroll").scrollTop = keep[1];
    paintWaves();
  }
  // Waveforms for ready clips, decoded once and cached.
  const waves = new Map();
  async function paintWaves() {
    for (const el of $("lanes").querySelectorAll("[data-clip]")) {
      const c = clipById(el.dataset.clip); if (!c || c.status !== "ready" || !c.url) continue;
      let peaks = waves.get(c.url);
      if (!peaks) {
        if (waves.has(c.url + "#loading")) continue; waves.set(c.url + "#loading", 1);
        try { const buf = await (await fetch(c.url)).arrayBuffer(); const ac = new (window.OfflineAudioContext || window.webkitOfflineAudioContext)(1, 2, 44100);
          const au = await ac.decodeAudioData(buf); const ch = au.getChannelData(0); const per = Math.floor(au.sampleRate / 50); peaks = [];
          for (let i = 0; i < ch.length; i += per) { let m = 0; for (let j = i; j < i + per && j < ch.length; j++) m = Math.max(m, Math.abs(ch[j])); peaks.push(m); }
          waves.set(c.url, peaks); paintTimeline(); } catch { /* not decodable */ }
        continue;
      }
      const cv = document.createElement("canvas"); const w = el.clientWidth, h = el.clientHeight; cv.width = w; cv.height = h; el.prepend(cv);
      const g = cv.getContext("2d"); g.fillStyle = "#000";
      const from = Math.floor(c.trim_s * 50), n = Math.floor((c.duration_s || 0) * 50);
      for (let px = 0; px < w; px++) { const p = peaks[from + Math.floor(px / w * n)] || 0; const hh = Math.max(1, p * h * 0.9); g.fillRect(px, (h - hh) / 2, 1, hh); }
    }
  }
  addEventListener("resize", () => S && paintTimeline());
  $("tl-zoom").oninput = () => { TL.zoom = +$("tl-zoom").value; $("tl-zoom-v").textContent = TL.zoom === 1 ? "fit" : TL.zoom + "×";
    const sc = $("tl-scroll"), mid = (sc.scrollLeft + sc.clientWidth / 2) / Math.max(1, sc.scrollWidth); paintTimeline(); sc.scrollLeft = mid * sc.scrollWidth - sc.clientWidth / 2; };
  $("tl-scroll").addEventListener("wheel", (e) => { if (!e.ctrlKey && !e.metaKey) return; e.preventDefault();
    $("tl-zoom").value = Math.max(1, Math.min(8, TL.zoom * (e.deltaY < 0 ? 1.25 : 0.8))); $("tl-zoom").oninput(); }, {passive: false});
  const hKey = "ed-tl-height";
  try { const h = +localStorage.getItem(hKey); if (h) ED.style.setProperty("--tlh", h + "px"); } catch { /* private mode */ }
  $("tl-grip").addEventListener("pointerdown", (e) => {
    const tl = $("tl"), y0 = e.clientY, h0 = tl.getBoundingClientRect().height; tl.classList.add("resizing"); $("tl-grip").setPointerCapture(e.pointerId);
    const mv = (ev) => { const h = Math.max(110, Math.min(innerHeight * 0.7, h0 + (y0 - ev.clientY))); ED.style.setProperty("--tlh", h + "px"); fitFrame(); };
    const up = () => { tl.classList.remove("resizing"); $("tl-grip").removeEventListener("pointermove", mv); $("tl-grip").removeEventListener("pointerup", up);
      try { localStorage.setItem(hKey, String(Math.round(tl.getBoundingClientRect().height))); } catch { /* private mode */ } paintTimeline(); };
    $("tl-grip").addEventListener("pointermove", mv); $("tl-grip").addEventListener("pointerup", up);
  });
  const tlKey = "ed-tl-collapsed";
  const setTl = (c) => { setTimeout(() => S && fitFrame(), 0); $("tl").classList.toggle("collapsed", c); $("tl-caret").textContent = c ? "▸" : "▾"; $("tl-toggle").setAttribute("aria-expanded", String(!c)); try { localStorage.setItem(tlKey, c ? "1" : ""); } catch { /* private mode */ } if (!c && S) paintTimeline(); };
  $("tl-toggle").onclick = () => setTl(!$("tl").classList.contains("collapsed"));
  try { if (localStorage.getItem(tlKey)) setTl(true); } catch { /* private mode */ }
  $("lanes").addEventListener("input", (e) => { const g = e.target.dataset.gain; if (g) { e.target.title = `${TRACK_NAME[g]} level ${e.target.value} dB`; } });
  $("lanes").addEventListener("change", async (e) => { const g = e.target.dataset.gain; if (!g) return;
    await api(`/api/editor/${PID}/settings`, {mix: {[g]: {gain_db: +e.target.value}}}); toast(`${TRACK_NAME[g]} ${e.target.value} dB`); load(true); });
  $("duck").onchange = async () => { await api(`/api/editor/${PID}/settings`, {mix: {duck_db: +$("duck").value}}); toast($("duck").value === "0" ? "Ducking off" : `Music ducks ${$("duck").value} dB under dialogue`); load(true); };

  // pointer: select / drag / trim / add
  let drag = null;
  $("lanes").addEventListener("pointerdown", (e) => {
    const t = TL.a + (e.clientX - e.target.closest(".ed-lane, .ed-lh, div").getBoundingClientRect().left) / TL.pps;
    if (e.target.closest("[data-mute]")) { const tr = e.target.closest("[data-mute]").dataset.mute;
      api(`/api/editor/${PID}/settings`, {mix: {[tr]: {mute: !S.mix[tr].mute}}}).then(() => load(true)); return; }
    const shotEl = e.target.closest("[data-tshot]");
    if (shotEl) {
      const id = shotEl.dataset.tshot; if (id !== sel.shot) selectShot(id);
      const el = $("lanes").querySelector(`[data-tshot="${id}"]`); if (!el) return;
      const x0 = e.clientX, left0 = parseFloat(el.style.left); let moved = false, mark = null;
      el.setPointerCapture(e.pointerId);
      const mv = (ev) => { const dx = ev.clientX - x0; if (Math.abs(dx) < 4 && !moved) return; moved = true; el.classList.add("dragging"); el.style.left = (left0 + dx) + "px";
        const t = TL.a + (left0 + dx + el.offsetWidth / 2) / TL.pps; const tgt = S.shots.filter((s) => s.id !== id).find((s) => t < s.start + s.seconds / 2);
        const at = tgt ? tgt.start : S.duration; if (!mark) { mark = document.createElement("div"); mark.className = "ed-tl-drop"; el.parentElement.appendChild(mark); }
        mark.style.left = (at - TL.a) * TL.pps + "px"; };
      const up = async (ev) => { el.removeEventListener("pointermove", mv); el.removeEventListener("pointerup", up); mark?.remove();
        if (!moved) return;
        const t = TL.a + (left0 + ev.clientX - x0 + el.offsetWidth / 2) / TL.pps;
        const others = S.shots.filter((s) => s.id !== id); const before = others.find((s) => t < s.start + s.seconds / 2);
        if (before) { const sc = sceneById(before.scene_id); await moveShot(id, sc.id, sc.shots.filter((x) => x !== id).indexOf(before.id)); }
        else { const last = others[others.length - 1]; const sc = sceneById(last ? last.scene_id : sel.scene); await moveShot(id, sc.id, sc.shots.filter((x) => x !== id).length); }
      };
      el.addEventListener("pointermove", mv); el.addEventListener("pointerup", up);
      return;
    }
    if (e.target.closest("#ruler")) { const r = $("ruler").getBoundingClientRect(); Player.seek(TL.a + (e.clientX - r.left) / TL.pps); return; }
    const blk = e.target.closest("[data-clip]");
    if (blk) {
      const c = clipById(blk.dataset.clip); sel.clip = c.id; setTab("shot");
      drag = {id: c.id, mode: e.target.classList.contains("rs") ? "trim" : "move", x0: e.clientX, start: c.start_s, dur: c.duration_s || 1, el: blk, moved: false};
      blk.setPointerCapture(e.pointerId); $("lanes").querySelectorAll(".sel").forEach((x) => x.classList.remove("sel")); blk.classList.add("sel");
      paintInspector(); return;
    }
    const lane = e.target.closest("[data-lane]");
    if (lane) { const r = lane.getBoundingClientRect(); addAudioPop(lane.dataset.lane, Math.max(0, TL.a + (e.clientX - r.left) / TL.pps), e.clientX, e.clientY); }
  });
  $("lanes").addEventListener("pointermove", (e) => {
    if (!drag) return; const dt = (e.clientX - drag.x0) / TL.pps; if (Math.abs(e.clientX - drag.x0) > 2) drag.moved = true;
    const snap = (v) => e.shiftKey ? v : Math.round(v * 10) / 10;
    if (drag.mode === "move") { drag.nstart = Math.max(0, snap(drag.start + dt)); drag.el.style.left = (drag.nstart - TL.a) * TL.pps + "px"; }
    else { const c = clipById(drag.id); const max = (c.source_s || 999) - c.trim_s; drag.ndur = Math.max(0.3, Math.min(max, snap(drag.dur + dt))); drag.el.style.width = drag.ndur * TL.pps + "px"; }
  });
  $("lanes").addEventListener("pointerup", async () => {
    if (!drag) return; const d = drag; drag = null; if (!d.moved) return;
    const ch = d.mode === "move" ? {start_s: d.nstart} : {duration_s: d.ndur};
    await change([{op: "edit_clip", clip_id: d.id, ...ch}], d.mode === "move" ? `Moved to ${fmt(d.nstart)}` : `Length ${d.ndur.toFixed(1)} s`);
  });

  function addAudioPop(track, t, cx, cy) {
    const pop = $("pop"); const sh = S.shots.find((s) => t >= s.start && t < s.start + s.seconds);
    const speakers = S.refs.filter((r) => ["character", "voice"].includes(r.kind));
    const head = `<div class="h"><span>${track === "dialogue" ? "🎙 A line of dialogue" : track === "music" ? "🎵 Music" : "🌧 Ambience / sound effects"} at ${fmt(t)}</span><button class="ed-btn ghost icon" data-x aria-label="Close">✕</button></div>`;
    const body = track === "dialogue"
      ? `<label class="ed-field">Who speaks<select id="ap-ref">${speakers.map((r) => `<option value="${r.id}">${esc(r.name)}${r.voice ? " (cloned voice)" : r.speaker ? " (" + esc(r.speaker) + ")" : " (default voice)"}</option>`).join("")}<option value="">Narrator / someone else</option></select></label>
         <label class="ed-field" id="ap-spk-wrap" ${speakers.length ? "hidden" : ""}>Voice<select id="ap-spk"><option value="">default</option>${S.speakers.map((s) => `<option>${esc(s)}</option>`).join("")}</select></label>
         <label class="ed-field">Line<textarea id="ap-text" rows="3" placeholder="What do they say?"></textarea></label>
         <label class="ed-field">Delivery (optional)<input id="ap-inst" placeholder="weeping, broken voice · whispered · commanding"></label>
         <label class="ed-switch"><input type="checkbox" id="ap-vid" checked> Use in video (performance / lip-sync shots under it hear it)</label>`
      : `<label class="ed-field">${track === "music" ? "Describe the music" : "Describe the sound"}<textarea id="ap-text" rows="3" placeholder="${track === "music" ? "slow, mournful bansuri over low strings, no drums" : "night palace: oil lamps crackling, distant temple bells, wind"}"></textarea></label>
         <div class="ed-row2"><label class="ed-field">Seconds<input id="ap-dur" type="number" min="10" max="240" value="${Math.round(Math.max(10, Math.min(120, (sceneSpan(sh?.scene_id || sel.scene)[1] - t) || 20)))}"></label>
         <label class="ed-field">…or upload a file<input id="ap-file" type="file" accept="audio/*"></label></div>
         <label class="ed-switch"><input type="checkbox" id="ap-vid"> Use in video (performance shots under it hear it)</label>`;
    pop.innerHTML = head + body + `<div class="acts"><button class="ed-btn ghost" data-x>Cancel</button><button class="ed-btn gold" id="ap-go">${track === "dialogue" ? "Speak it" : "Generate"}</button></div>`
      + `<div class="ed-note">${sh ? `Under shot “${esc(sh.title || "shot " + sh.n)}” (${MAKE[sh.make][0]}).` : "After the last shot."} Generated on the studio's GPU; it appears on the lane when ready.</div>`;
    pop.hidden = false;
    const r = pop.getBoundingClientRect();
    pop.style.left = Math.min(innerWidth - r.width - 10, Math.max(10, cx - 40)) + "px";
    pop.style.top = Math.max(10, cy - r.height - 12) + "px";
    pop.querySelectorAll("[data-x]").forEach((b) => b.onclick = () => pop.hidden = true);
    if ($("ap-ref")) $("ap-ref").onchange = () => { $("ap-spk-wrap").hidden = !!$("ap-ref").value && !!refById($("ap-ref").value); };
    $("ap-text").focus();
    $("ap-go").onclick = async () => {
      try {
        if (track === "dialogue") {
          const text = $("ap-text").value.trim(); if (!text) { toast("Write the line"); return; }
          await api(`/api/editor/${PID}/clips`, {kind: "dialogue", ref_id: $("ap-ref").value || null, speaker: $("ap-spk")?.value || null, text, instruct: $("ap-inst").value, start_s: t, use_in_video: $("ap-vid").checked});
        } else if ($("ap-file").files[0]) {
          const fd = new FormData(); fd.append("file", $("ap-file").files[0]); fd.append("track", track); fd.append("start_s", t);
          await api(`/api/editor/${PID}/clips`, fd);
        } else {
          const d = $("ap-text").value.trim(); if (!d) { toast("Describe it, or choose a file"); return; }
          await api(`/api/editor/${PID}/clips`, {kind: track, description: d, start_s: t, duration: +$("ap-dur").value, use_in_video: $("ap-vid").checked});
        }
        pop.hidden = true; toast(track === "dialogue" ? "Speaking… it lands on the dialogue lane" : "Generating… it lands on the lane when ready"); load(true);
      } catch (e) { toast(e.message); }
    };
  }

  // ------------------------------------------------------------------ inspector
  document.querySelectorAll(".ed-tabs button").forEach((b) => b.onclick = () => setTab(b.dataset.tab));
  // Phones: one section at a time, chosen from the bottom tab bar (2026-11-25: "very
  // non-responsive. User cannot scroll"). Each section is the only thing that scrolls.
  const phone = () => matchMedia("(max-width: 780px)").matches;
  function setM(m) {
    ED.dataset.m = m;
    document.querySelectorAll("#mtabs button").forEach((b) => b.classList.toggle("on", b.dataset.m === m));
    if (m === "shot" || m === "agent") { if (tab !== m) setTab(m); }
    if (m === "timeline" && view === "assets") setView("storyboard");
    if (m === "timeline" || m === "stage") requestAnimationFrame(() => { paint(); if (typeof fitFrame === "function") fitFrame(); });
  }
  document.querySelectorAll("#mtabs button").forEach((b) => b.onclick = () => setM(b.dataset.m));
  ED.dataset.m = "stage";
  function setTab(t) {
    if (phone() && ED.dataset.m !== t && (t === "shot" || t === "agent")) { ED.dataset.m = t;
      document.querySelectorAll("#mtabs button").forEach((b) => b.classList.toggle("on", b.dataset.m === t)); }
    tab = t;
    document.querySelectorAll(".ed-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
    $("pane-shot").hidden = t !== "shot"; $("pane-agent").hidden = t !== "agent";
    if (t === "agent") ensureChat();
    paintInspectorHead();
  }
  function paintInspectorHead() {
    const sh = shotById(sel.shot), c = clipById(sel.clip);
    $("insp-title").textContent = tab === "agent" ? (scope === "shot" && sh ? `Agent · Shot ${String(sceneShots(sh.scene_id).indexOf(sh) + 1).padStart(2, "0")}` : scope === "scene" && sceneById(sel.scene) ? `Agent · ${sceneById(sel.scene).title}` : "Agent · Whole story")
      : c ? TRACK_NAME[c.track] + " clip" : sh ? `Shot ${String(sceneShots(sh.scene_id).indexOf(sh) + 1).padStart(2, "0")}` : sceneById(sel.scene)?.title || "Scene";
    $("insp-more").hidden = tab === "agent";
  }
  $("insp-more").onclick = async () => {
    const c = clipById(sel.clip), sh = shotById(sel.shot);
    if (c) { if (confirm(`Remove “${c.name}” from the timeline?`)) { sel.clip = null; await change([{op: "delete_clip", clip_id: c.id}], "Clip removed"); } return; }
    if (sh) { if (confirm("Delete this shot and its clip?")) { sel.shot = null; await change([{op: "delete_shot", shot_id: sh.id}], "Shot deleted"); } return; }
    const sc = sceneById(sel.scene);
    if (sc && confirm(`Delete the scene “${sc.title}” and all its shots?`)) { sel.scene = null; await change([{op: "delete_scene", scene_id: sc.id}], "Scene deleted"); }
  };

  function paintInspector() {
    paintInspectorHead();
    if (tab !== "shot") return;
    const pane = $("pane-shot"), sh = shotById(sel.shot), c = clipById(sel.clip), sc = sceneById(sel.scene);
    let html = proposalsHtml();
    if (c) html += clipInspector(c);
    if (sh) html += shotInspector(sh);
    else if (sc) html += sceneInspector(sc);
    pane.innerHTML = html;
    wireInspector(sh, c, sc);
  }

  function sceneInspector(sc) {
    return `<div class="ed-group"><label class="ed-field">Scene title<input data-scene-f="title" value="${esc(sc.title)}" maxlength="120"></label></div>
      <div class="ed-group"><label class="ed-field">What happens (the agent reads this for every shot in the scene)<textarea data-scene-f="summary" rows="4">${esc(sc.summary)}</textarea></label></div>
      <div class="ed-group"><button class="ed-btn gold" id="sc-add">＋ Add a shot</button> <button class="ed-btn" id="sc-ask">✨ Plan the shots with the agent</button> <button class="ed-btn" id="sc-more">✎ Assets & lock</button></div>`;
  }

  // The settings a use case needs, right under the picker.
  function useOptions(sh) {
    const o = sh.opts || {};
    const audioClips = S.clips.filter((c) => ["dialogue", "music"].includes(c.track) && c.status === "ready");
    const voiceRow = (i, label) => { const cur = (o.speakers || [])[i] || {};
      return `<div class="ed-voice"><span class="l">${label}</span><select data-voice="${i}" aria-label="${label}"><option value="">— choose a line or song —</option>${audioClips.map((c) => `<option value="${c.id}" ${cur.clip === c.id ? "selected" : ""}>${c.track === "music" ? "🎵" : "🎙"} ${esc(c.name || c.text || "clip")}${c.duration_s ? ` · ${c.duration_s.toFixed(1)}s` : ""}</option>`).join("")}</select><button class="ed-btn sm" type="button" data-voiceup="${i}">⤒ Upload</button></div>`; };
    if (sh.make === "talk2") return `<div class="ed-group ed-opts"><h3>The two voices</h3>
      ${voiceRow(0, (o.layout === "right_left" ? "Right" : "Left") + " person · speaks first")}${voiceRow(1, (o.layout === "right_left" ? "Left" : "Right") + " person · answers")}
      <label class="ed-check"><input type="checkbox" id="opt-layout" ${o.layout === "right_left" ? "checked" : ""}> The person on the right speaks first</label>
      <div class="ed-note">An uploaded voice goes on the Dialogue lane under this shot, so you can see and mix it. The shot lasts as long as both voices.</div></div>`;
    if (sh.make === "longtalk") return `<div class="ed-group ed-opts"><h3>The voice or song</h3>${voiceRow(0, "Voice")}
      <div class="ed-note">Up to 60 s. An uploaded file goes on the Dialogue lane under this shot. The shot lasts as long as the voice.</div></div>`;
    if (sh.make === "recast") { const chars = S.refs.filter((r) => r.kind === "character");
      const cast = Object.fromEntries((o.cast || []).map((c) => [c.ref, c.who]));
      return `<div class="ed-group ed-opts"><h3>Who replaces whom</h3>${chars.length ? chars.map((r) => `<label class="ed-cast"><span>${esc(r.name)} replaces</span><input data-cast="${r.id}" value="${esc(cast[r.id] || "")}" placeholder="e.g. the man in the grey jacket"></label>`).join("") : `<div class="ed-note">Add your characters under References first.</div>`}
        <div class="ed-note">Up to 3 people. Leave a line empty to leave that character out. Their face close-up and own photos keep the face right.</div></div>`; }
    if (sh.make === "restyle") { const looks = S.refs.filter((r) => sh.refs.includes(r.id) && r.image);
      return `<div class="ed-group ed-opts"><h3>What changes</h3><textarea id="opt-change" rows="3" placeholder="e.g. it is a rainy night with neon lights; everyone wears winter coats">${esc(o.change || "")}</textarea>
      <div class="ed-note" style="margin-top:6px">${looks.length ? `Looks like: <b>${looks.slice(0, 3).map((r) => esc(r.name)).join(", ")}</b> (this shot's references).` : "To show the look with a picture (an outfit, a style, a place), add it to this shot's References below — up to 3 are used."}</div></div>`; }
    return "";
  }
  function bindUseOptions(sh, pane) {
    const setOpts = (opts, msg) => change([{op: "edit_shot", shot_id: sh.id, opts}], msg);
    pane.querySelectorAll("[data-voice]").forEach((sel) => sel.onchange = () => {
      const sp = [...((sh.opts || {}).speakers || [])]; while (sp.length < 2) sp.push({});
      sp[+sel.dataset.voice] = {...sp[+sel.dataset.voice], clip: sel.value || null};
      setOpts({speakers: sh.make === "talk2" ? sp : sp.slice(0, 1)}, "Voice set");
    });
    pane.querySelectorAll("[data-voiceup]").forEach((b) => b.onclick = () => { const inp = document.createElement("input"); inp.type = "file"; inp.accept = "audio/*";
      inp.onchange = async () => { if (!inp.files[0]) return; const fd = new FormData(); fd.append("file", inp.files[0]); fd.append("index", b.dataset.voiceup); toast("Uploading the voice…");
        try { await api(`/api/editor/${PID}/shots/${sh.id}/speaker-audio`, fd); toast("Voice added — it's on the Dialogue lane under this shot"); load(true); } catch (e) { toast(e.message); } };
      inp.click(); });
    if ($("opt-layout")) $("opt-layout").onchange = () => setOpts({layout: $("opt-layout").checked ? "right_left" : "left_right"}, "Speaking order set");
    pane.querySelectorAll("[data-cast]").forEach((inp) => inp.onchange = () => {
      const cast = [...pane.querySelectorAll("[data-cast]")].filter((x) => x.value.trim()).map((x) => ({ref: x.dataset.cast, who: x.value.trim()}));
      setOpts({cast}, "Cast set");
    });
    if ($("opt-change")) $("opt-change").onchange = () => setOpts({change: $("opt-change").value.trim()}, "Change set");
  }

  function shotInspector(sh) {
    const cap = S.limits[sh.make];
    const refChips = S.refs.map((r) => `<div class="ed-chip ${sh.refs.includes(r.id) ? "" : "off"}" data-tref="${r.id}" title="${sh.refs.includes(r.id) ? "In this shot — click to remove" : "Click to add to this shot"}">
      <div class="th">${r.image ? `<img src="${esc(r.image)}" alt="">` : ({voice: "🎙", location: "🏛"}[r.kind] || "👤")}</div><span>${esc(r.name)}</span><button aria-label="toggle">${sh.refs.includes(r.id) ? "✕" : "＋"}</button></div>`).join("");
    const lines = S.clips.filter((c) => c.track === "dialogue" && c.start_s < sh.start + sh.seconds && c.start_s + (c.duration_s || 0) > sh.start);
    const gen = (key, icon, label, small) => `<button type="button" data-gen="${key}"><span class="i">${icon}</span><span>${label}</span><span class="a">${genOpen === key ? "⌄" : "›"}</span>${small ? `<small>${small}</small>` : ""}</button>` + (genOpen === key ? genForm(key, sh) : "");
    const snd = soundUnder(sh);
    const silent = !snd.tracks.length && ["animate", "motion"].includes(sh.make);
    const silentCard = silent ? `<div class="ed-silent"><div class="h">🔇 This shot has no sound</div>
      <div class="ed-note">Sound is always made separately: a voice first, then the video from that voice.</div>
      <div class="opts">
        <button type="button" data-sound="line"><span>🎙</span>Give it a voice<small>the line is voiced first, then the video is generated lip-synced to it</small></button>
        <button type="button" data-sound="music"><span>🎵</span>Music or ambience<small>score or a soundscape under this scene, mixed in</small></button>
        <button type="button" data-sound="model"><span>🎭</span>Let the video make its own sound<small>the video model invents the sound with the picture — not the character's voice</small></button>
      </div></div>` : "";
    const inScene = sceneShots(sh.scene_id), idx = inScene.findIndex((x) => x.id === sh.id);
    const moveRow = `<div class="ed-move"><label class="ed-field">Scene<select id="mv-scene">${S.scenes.map((sc, i) => `<option value="${sc.id}" ${sc.id === sh.scene_id ? "selected" : ""}>${i + 1}. ${esc(sc.title || "Untitled scene")}</option>`).join("")}</select></label>
      <button class="ed-btn icon" id="mv-up" type="button" title="Move earlier (Alt+←)" aria-label="Move earlier">◀</button><span class="ed-note" style="padding-bottom:9px;white-space:nowrap">${idx + 1} of ${inScene.length}</span><button class="ed-btn icon" id="mv-down" type="button" title="Move later (Alt+→)" aria-label="Move later">▶</button></div>`;
    const picker = `<div class="ed-group"><h3>What happens in this shot?</h3><div class="ed-uses" role="radiogroup" aria-label="What happens in this shot">${USES.map((u) => `<button type="button" role="radio" aria-checked="${sh.make === u.make}" data-make="${u.make}" class="ed-use ${sh.make === u.make ? "on" : ""}"><span class="i">${u.icon}</span><span class="b"><b>${u.title}</b><small>${u.what}</small><small class="m">${u.needs ? "Needs " + u.needs + " · " : ""}${u.model}</small></span></button>`).join("")}</div>
        <div class="ed-note" style="margin-top:6px">${sh.make === "motion" ? (sh.ref_videos.length ? `<b>Copies ${esc(sh.ref_videos[0].label)}.</b>` : ` <b style='color:var(--warn)'>Add the video to copy under “Reference videos” below.</b>`) : ""}${sh.make === "perform" ? ` <b>${(sh.refs.length ? sh.refs.length + " reference(s) in this shot" : "No references in this shot")}</b>, plus its scene's.` : ""}${sh.make === "lipsync" ? (lines.length ? ` Under it: ${lines.map((l) => esc(l.name)).join("; ")}${lines.some((l) => !l.use_in_video) ? " (some not marked “use in video”)" : ""}.` : " <b style='color:var(--warn)'>No dialogue under this shot yet</b> — click the Dialogue lane under it.") : ""}</div></div>`;
    return picker + useOptions(sh) + silentCard + `<div class="ed-group">
      <label class="ed-field">Title<input data-f="title" value="${esc(sh.title)}" maxlength="80" placeholder="Name this shot"></label></div>${moveRow}
      <div class="ed-row2">
        <label class="ed-field">Framing<select data-f="framing">${S.framings.map((f) => `<option value="${f}" ${f === sh.framing ? "selected" : ""}>${esc(f ? f + " · " + FRAMING[f] : "—")}</option>`).join("")}</select></label>
        <label class="ed-field">Seconds (max ${cap})<input data-f="seconds" type="number" min="1" max="${cap}" value="${sh.seconds}"></label></div>
      <label class="ed-field">Opening picture<select data-f="start_from">
        <option value="" ${sh.start_from ? "" : "selected"}>Its own picture</option>
        <option value="prev_last" ${sh.start_from === "prev_last" ? "selected" : ""} ${S.shots[0]?.id === sh.id ? "disabled" : ""}>Last frame of the previous shot (continuous)</option></select></label>
      <label class="ed-field">Prompt — what moves, how the camera moves<textarea data-f="prompt" rows="4" placeholder="What moves, and how the camera moves…">${esc(sh.prompt)}</textarea></label>
      ${(sh.opts || {})._polish ? `<details class="ed-polish"><summary>Prompt sent to the video model · written by the local ${esc(S.planner || "27B")}</summary><pre>${esc(sh.opts._polish.prompt)}${sh.opts._polish.motion_prompt ? "\n\nMotion: " + esc(sh.opts._polish.motion_prompt) : ""}</pre><div class="ed-note">From the last render. It is written again when you change this shot.</div></details>` : `<div class="ed-note" style="margin:-4px 0 8px">At render, the local planner model rewrites this into the final prompt for the video model.</div>`}
      <div class="ed-group"><h3>References <span class="c ed-note">${sh.refs.length} in this shot</span></h3>
        ${S.refs.length ? `<div class="ed-chips">${refChips}</div>` : `<div class="ed-note">No references yet. <a href="#" id="ins-addref" style="color:var(--gold)">Add a character or place</a> so it stays consistent across shots.</div>`}</div>
      ${["lipsync", "talk2", "longtalk"].includes(sh.make) ? "" : `<div class="ed-group"><h3>${sh.make === "motion" ? "The video to copy" : ["recast", "restyle"].includes(sh.make) ? "The video to remake" : "Reference videos"} <span class="c ed-note">${sh.ref_videos.length} of ${["motion", "recast", "restyle"].includes(sh.make) ? 1 : 3}</span></h3>
        ${sh.ref_videos.length ? `<div class="ed-rvids">${sh.ref_videos.map((v) => `<div class="ed-rvid"><span>🎞 &lt;Video ${v.i + 1}&gt; ${esc(v.label)}</span><button type="button" data-rvdel="${v.i}" aria-label="Remove">✕</button></div>`).join("")}</div>` : ""}
        ${sh.ref_videos.length < (["motion", "recast", "restyle"].includes(sh.make) ? 1 : 3) ? `<div class="ed-rvadd"><select id="rv-shot" aria-label="Use a shot's clip"><option value="">＋ A shot's clip…</option>${S.shots.filter((x) => x.clip && x.id !== sh.id && !sh.ref_videos.some((v) => v.label.includes("“" + (x.title || "shot " + x.n) + "”"))).map((x) => `<option value="${x.id}">${x.n}. ${esc(x.title || "Shot " + x.n)}</option>`).join("")}</select><button class="ed-btn sm" id="rv-upload" type="button">⤒ Upload video</button></div>` : ""}
        <div class="ed-note">${["recast", "restyle"].includes(sh.make) ? "Its motion, camera, timing and sound are kept. 4–14 s is used; a shorter video loops." : sh.make === "motion" ? "The person in the shot's picture makes these exact movements. Up to 15 s is used; a shorter video loops." : "A loose guide for motion and camera. For an exact copy of a dance or gesture, choose “🕺 Copy a dance or movement from a video” at the top. Up to 15 s is used."}</div></div>`}
      <div class="ed-group"><h3>Generate</h3><div class="ed-gen">
        ${sh.picture ? gen("edit", "✏️", "Edit this picture", "say what to change; everything else stays") : ""}
        ${gen("image", "🖼", "Image", sh.picture ? "new picture options (keeps the references)" : "this shot has no picture yet")}
        ${gen("video", "🎬", "Video", sh.status === "done" ? "clip ready — render again" : sh.status === "stale" ? "changed since rendered" : "render this shot's clip")}
        ${gen("voice", "🎙", "Voice", "a line of dialogue at this shot")}
        ${gen("music", "🎵", "Music", "score for this scene")}
        ${gen("sfx", "🌧", "SFX / ambience", "a soundscape under this scene")}
      </div></div>
      <div class="ed-group" style="display:flex;gap:6px;flex-wrap:wrap"><button class="ed-btn sm" id="ins-upload">⤒ Upload picture</button>${sh.clip ? `<a class="ed-btn sm" href="${esc(sh.clip)}" download>⤓ Clip</a>` : ""}${window.BATIYAO_ON && (sh.clip || sh.picture) ? `<button class="ed-btn sm" type="button" data-bt-share="${sh.clip ? "shot" : "shotpic"}:${PID}:${sh.id}">↗ Share</button>` : ""}<button class="ed-btn sm" id="ins-ask">✨ Ask about this shot</button></div>`;
  }

  function genForm(key, sh) {
    if (key === "edit") return `<div class="ed-genform"><label class="ed-field">What should change?<textarea id="g-edit" rows="3" placeholder="e.g. she is not smiling · evening light instead of noon · remove the car behind him · he holds the cup in his left hand"></textarea></label>
      <div class="ed-row2"><label class="ed-field">Versions<select id="g-ecount"><option>1</option><option selected>2</option><option>3</option></select></label>
      <label class="ed-field">Model<select id="g-emodel"><option value="qwen-image-edit-2511">Qwen-Image-Edit — made for edits (keeps faces)</option>${modelOptions("").replace(/<option value="">[^<]*<\/option>/, "").replace(/<option value="qwen-image-edit-2511"[^>]*>[^<]*<\/option>/, "")}</select></label></div>
      <div class="acts"><button class="ed-btn gold" data-go="edit">Edit the picture</button></div><div class="ed-note">Only what you describe changes. The edited versions appear under Proposed changes to pick from; the current picture stays until you pick one.</div></div>`;
    if (key === "image") return `<div class="ed-genform"><label class="ed-field">Picture prompt (empty = from the shot's framing, title, prompt and scene)<textarea id="g-prompt" rows="3"></textarea></label>
      <div class="ed-row2"><label class="ed-field">Options<select id="g-count"><option>1</option><option selected>2</option><option>3</option><option>4</option></select></label>
      <label class="ed-field">Model<select id="g-model">${modelOptions("")}</select></label></div>
      <div class="acts"><button class="ed-btn gold" data-go="image">Generate options</button></div><div class="ed-note">Uses this shot's references as source pictures. You pick one under Proposed changes.</div></div>`;
    if (key === "video") return `<div class="ed-genform"><div class="ed-note">${MAKE[sh.make][0]} · ${sh.seconds} s · about ${Math.ceil(sh.seconds / 5 * 2)} min of GPU. ${!sh.picture && sh.start_from !== "prev_last" ? "No picture yet: draw it first (🖼 Pictures)." : sh.start_from === "prev_last" ? "Opens on the previous shot's last frame." : ""} ${!sh.prompt ? "<b style='color:var(--warn)'>Needs a prompt.</b>" : ""}</div>
      <div class="acts"><button class="ed-btn" data-go="video-scene">Render whole scene</button><button class="ed-btn gold" data-go="video">Render this shot</button></div></div>`;
    if (key === "voice") { const sp = S.refs.filter((r) => ["character", "voice"].includes(r.kind) && sh.refs.includes(r.id)).concat(S.refs.filter((r) => ["character", "voice"].includes(r.kind) && !sh.refs.includes(r.id)));
      return `<div class="ed-genform"><label class="ed-field">Who<select id="g-ref">${sp.map((r) => `<option value="${r.id}">${esc(r.name)}</option>`).join("")}<option value="">Narrator / default voice</option></select></label>
      <label class="ed-field">Line<textarea id="g-text" rows="3"></textarea></label><label class="ed-field">Delivery<input id="g-inst" placeholder="trembling, quiet"></label>
      <label class="ed-switch"><input type="checkbox" id="g-vid" checked> Use in video</label>
      ${sh.picture || sh.start_from === "prev_last" ? `<label class="ed-switch"><input type="checkbox" id="g-lip" checked> Then generate the video with this voice (lip-synced) — starts as soon as the voice is ready</label>` : `<div class="ed-note">Give the shot a picture, then its video can be generated with this voice.</div>`}
      <div class="acts"><button class="ed-btn gold" data-go="voice">Speak it at ${fmt(sh.start + 0.3)}</button></div></div>`; }
    const mus = key === "music";
    const [a, b] = sceneSpan(sh.scene_id);
    return `<div class="ed-genform"><label class="ed-field">${mus ? "Music" : "Sound"}<textarea id="g-text" rows="3" placeholder="${mus ? "mournful bansuri and tanpura drone, slow" : "palace at night, oil lamps, distant bells"}"></textarea></label>
      <div class="ed-row2"><label class="ed-field">Seconds<input id="g-dur" type="number" min="10" max="240" value="${Math.max(10, Math.round(b - a))}"></label><label class="ed-switch" style="align-self:end"><input type="checkbox" id="g-vid"> Use in video</label></div>
      <div class="acts"><button class="ed-btn gold" data-go="${key}">Generate for this scene</button></div></div>`;
  }

  function clipInspector(c) {
    const ref = c.ref_id ? refById(c.ref_id) : null;
    return `<div class="ed-clipins"><div class="h"><i style="background:${TRACK_COLOR[c.track]}"></i><span style="flex:1">${esc(c.name)}</span><button class="ed-btn ghost sm" id="ci-close">✕</button></div>
      ${c.status === "script" ? `<div class="ed-note">📝 Script line — ${c.auto ? "being voiced in the character's voice…" : "not voiced yet."}${c.error ? ` <span style="color:var(--bad)">${esc(c.error)}</span>` : ""}</div>${c.auto ? "" : `<button class="ed-btn sm gold" type="button" id="ci-voice">🎙 Voice it now</button>`}`
        : c.status === "pending" ? `<div class="ed-note">⏳ Voicing…</div>` : c.status === "failed" ? `<div class="ed-note" style="color:var(--bad)">${esc(c.error || "failed")}</div>` : c.url ? `<audio src="${esc(c.url)}" controls style="width:100%"></audio>` : ""}
      ${ref ? `<div class="ed-note">Spoken by ${esc(ref.name)}${c.text ? `: “${esc(c.text)}”` : ""}</div>` : ""}
      <div class="ed-row2"><label class="ed-field">Start (s)<input data-c="start_s" type="number" step="0.1" min="0" value="${c.start_s}"></label><label class="ed-field">Length (s)<input data-c="duration_s" type="number" step="0.1" min="0.2" value="${c.duration_s ?? ""}"></label></div>
      <div class="ed-range"><span>Trim start</span><input type="range" data-c="trim_s" min="0" max="${Math.max(0.1, (c.source_s || 1) - 0.2)}" step="0.1" value="${c.trim_s}"><output>${c.trim_s.toFixed(1)} s</output></div>
      <div class="ed-range"><span>Gain</span><input type="range" data-c="gain_db" min="-30" max="12" step="0.5" value="${c.gain_db}"><output>${c.gain_db} dB</output></div>
      <div class="ed-range"><span>Fade in</span><input type="range" data-c="fade_in" min="0" max="10" step="0.1" value="${c.fade_in}"><output>${c.fade_in.toFixed(1)} s</output></div>
      <div class="ed-range"><span>Fade out</span><input type="range" data-c="fade_out" min="0" max="10" step="0.1" value="${c.fade_out}"><output>${c.fade_out.toFixed(1)} s</output></div>
      <label class="ed-switch"><input type="checkbox" data-c="use_in_video" ${c.use_in_video ? "checked" : ""}> Use in video generation — performance and lip-sync shots under it get it as audio</label>
      <label class="ed-field">Track<select data-c="track">${["dialogue", "ambience", "music"].map((t) => `<option value="${t}" ${t === c.track ? "selected" : ""}>${TRACK_NAME[t]}</option>`).join("")}</select></label></div>`;
  }

  function describeChange(c) {
    const sh = c.shot_id ? shotById(c.shot_id) : null, sc = c.scene_id ? sceneById(c.scene_id) : null;
    const nm = sh ? `“${sh.title || "shot " + sh.n}”` : sc ? `“${sc.title}”` : "";
    switch (c.op) {
      case "set_story": return c.title ? `Retitle to “${esc(c.title)}”` + (c.story ? " and rewrite the story" : "") : "Rewrite the story";
      case "add_scene": return `New scene “${esc(c.title || "")}”` + (c.summary ? ` — ${esc(c.summary.slice(0, 90))}` : "");
      case "edit_scene": return `Change scene ${esc(nm)}` + (c.title ? ` → “${esc(c.title)}”` : "") + (c.position != null ? ` (move to ${c.position + 1})` : "");
      case "delete_scene": return `Delete scene ${esc(nm)}`;
      case "add_shot": return `New shot ${c.framing ? esc(c.framing) + " " : ""}“${esc(c.title || "")}”${c.seconds ? ` · ${c.seconds}s` : ""}${c.make && c.make !== "animate" ? " · " + MAKE[c.make]?.[0] : ""}`;
      case "edit_shot": return `Shot ${esc(nm)}: ` + Object.keys(c).filter((k) => !["op", "shot_id"].includes(k)).map((k) => k === "prompt" ? `prompt → “${esc(String(c[k]).slice(0, 80))}…”` : `${k} → ${esc(JSON.stringify(c[k]))}`).join(", ");
      case "delete_shot": return `Delete shot ${esc(nm)}`;
      case "edit_clip": { const cl = clipById(c.clip_id); return `Clip “${esc(cl?.name || c.clip_id)}”: ` + Object.keys(c).filter((k) => !["op", "clip_id"].includes(k)).map((k) => `${k} ${esc(JSON.stringify(c[k]))}`).join(", "); }
      case "delete_clip": return `Remove clip “${esc(clipById(c.clip_id)?.name || c.clip_id)}”`;
      case "set_mix": return "Mix: " + Object.keys(c).filter((k) => k !== "op").map((k) => `${k} ${esc(JSON.stringify(c[k]))}`).join(", ");
      case "add_ref": return `New ${esc(c.kind || "reference")} “${esc(c.name)}”`;
      case "edit_ref": return `Change reference “${esc(refById(c.ref_id)?.name || c.ref_id)}”`;
      default: return esc(c.op);
    }
  }

  // ------------------------------------------------------------------ review a proposal
  // 2026-11-25, the person: "clicking on review should simply launch a clear place to review
  // the change" -- it used to jump to the Shot tab, where the proposal often wasn't shown.
  const FIELD = {title: "Title", prompt: "What happens", seconds: "Seconds", framing: "Framing", make: "Use case",
    refs: "Characters & places", start_from: "Opening picture", scene_id: "Scene", position: "Position", opts: "Settings"};
  const showVal = (k, v) => k === "make" ? (USES.find((u) => u.make === v)?.title || v)
    : k === "refs" ? (v || []).map((r) => refById(r)?.name || r).join(", ") || "none"
    : k === "framing" ? (v ? v + " · " + (FRAMING[v] || "") : "—")
    : k === "start_from" ? (v === "prev_last" ? "last frame of the previous shot" : "its own picture")
    : k === "scene_id" ? (sceneById(v)?.title || v) : typeof v === "object" ? JSON.stringify(v) : String(v ?? "");
  function reviewChanges(p) {
    const keyTitle = {};                       // new scenes, by their key, for grouping
    p.changes.forEach((c) => { if (c.op === "add_scene" && c.key) keyTitle[c.key] = c.title; });
    const groups = [], byScene = {};
    const group = (name) => byScene[name] || (byScene[name] = (groups.push({name, items: []}), groups[groups.length - 1]));
    for (const c of p.changes) {
      if (c.op === "add_scene") { group(`🎬 New scene: ${c.title || "untitled"}`).items.push(
        `<div class="rv-sum">${esc(c.summary || "")}${(c.refs || []).length ? `<div class="rv-meta">With: ${esc((c.refs || []).map((r) => refById(r)?.name || r).join(", "))}</div>` : ""}</div>`); continue; }
      if (c.op === "add_shot") {
        const g = keyTitle[c.scene_id] ? `🎬 New scene: ${keyTitle[c.scene_id]}` : `🎬 ${sceneById(c.scene_id)?.title || "Shots"}`;
        group(g).items.push(`<div class="rv-shot"><div class="rv-h"><b>${esc(c.title || "New shot")}</b><span>${esc([c.framing, c.seconds ? c.seconds + " s" : "", c.make && c.make !== "animate" ? MAKE[c.make]?.[0] : ""].filter(Boolean).join(" · "))}</span></div>
          ${c.prompt ? `<div class="rv-p">${esc(c.prompt)}</div>` : ""}${(c.dialogue || []).length ? `<div class="rv-meta">Lines: ${(c.dialogue || []).map((d) => `<i>${esc(d.speaker || "")}</i> “${esc(d.text || d.line || "")}”`).join(" · ")}</div>` : ""}</div>`); continue; }
      if (c.op === "edit_shot") {
        const sh = shotById(c.shot_id);
        const rows = Object.keys(c).filter((k) => !["op", "shot_id", "key"].includes(k)).map((k) =>
          `<tr><th>${FIELD[k] || esc(k)}</th><td class="was">${esc(sh ? showVal(k, sh[k]) : "")}</td><td class="now">${esc(showVal(k, c[k]))}</td></tr>`).join("");
        group(`✏️ Shot “${sh ? sh.title || "shot " + sh.n : c.shot_id}”`).items.push(`<table class="rv-diff"><thead><tr><th></th><th>Now</th><th>Proposed</th></tr></thead><tbody>${rows}</tbody></table>`); continue; }
      if (c.op === "add_ref") { group("👤 New characters & places").items.push(`<div class="rv-shot"><div class="rv-h"><b>${esc(c.name)}</b><span>${esc(c.kind || "")}</span></div><div class="rv-p">${esc(c.description || "")}</div></div>`); continue; }
      group("Other changes").items.push(`<div class="rv-p">${describeChange(c)}</div>`);
    }
    return groups.map((g) => `<section class="rv-group"><h3>${esc(g.name)}</h3>${g.items.join("")}</section>`).join("");
  }
  function openReview(id) {
    const p = S.proposals.find((x) => x.id === id);
    if (!p) { toast("That proposal has already been applied or dismissed"); return; }
    const pick = () => pickedOption[p.id] || p.options.find((o) => o.status === "succeeded")?.image_id;
    const sh = p.shot_id ? shotById(p.shot_id) : null;
    const optsHtml = () => p.options.length ? `<div class="rv-opts">${p.options.map((o, i) => `<button type="button" class="rv-opt ${pick() === o.image_id ? "on" : ""}" data-rvopt="${o.image_id}" ${o.status !== "succeeded" ? "disabled" : ""}>
        ${o.status === "succeeded" ? `<img src="${esc(o.url)}" alt="option ${i + 1}">` : `<span>${o.status === "failed" ? "failed" : "drawing…"}</span>`}<em>Option ${i + 1}</em></button>`).join("")}</div>
        <div class="ed-note">Tap the picture to use.</div>` : "";
    const n = p.changes.length;
    modal(`<div class="rv"><div class="rv-top"><h2>✦ ${esc(p.title)}</h2><button class="ed-btn ghost icon" data-act="close" aria-label="Close">✕</button></div>
      ${sh ? `<div class="rv-meta">For shot “${esc(sh.title || "shot " + sh.n)}”</div>` : ""}
      ${p.summary ? `<div class="rv-sumtop">${esc(p.summary)}</div>` : ""}
      ${optsHtml()}
      ${n ? `<div class="rv-count">${n} change${n === 1 ? "" : "s"}</div>${reviewChanges(p)}` : ""}
      <div class="acts rv-acts"><button class="ed-btn ghost" data-act="dismiss">Dismiss</button><span style="flex:1"></span>
        <button class="ed-btn" data-act="apply">Apply</button><button class="ed-btn gold" data-act="applygen" title="Apply, then make the videos of the shots it touches">Apply & make videos</button></div></div>`,
      async (act) => {
        if (act === "close") return true;
        if (act === "dismiss") { await api(`/api/editor/${PID}/proposals/${p.id}/dismiss`, {}); toast("Dismissed"); load(true); return true; }
        if (act === "apply" || act === "applygen") {
          const opt = pick() || null;
          if (p.options.length && !opt) { toast("The pictures are still being made"); return false; }
          try { const r = await api(`/api/editor/${PID}/proposals/${p.id}/apply`, {option: opt, render: act === "applygen"});
            toast((r.rendering ? `Applied — making ${r.rendering.length} video(s)` : "Applied") + (r.failed?.length ? ` · ${r.failed.length} skipped: ${r.failed[0]}` : "")); load(true); return true;
          } catch (e) { toast(e.message); return false; }
        }
        return false;
      });
    $("sheet").classList.add("wide", "review");
    $("sheet").addEventListener("click", (e) => { const b = e.target.closest("[data-rvopt]"); if (!b) return;
      pickedOption[p.id] = b.dataset.rvopt; $("sheet").querySelectorAll("[data-rvopt]").forEach((x) => x.classList.toggle("on", x === b)); });
  }

  function proposalsHtml() {
    const props = S.proposals.slice().sort((a, b) => (b.shot_id === sel.shot) - (a.shot_id === sel.shot));
    if (!props.length) return "";
    return `<div class="ed-group"><h3>✨ Proposed changes <span class="c">${props.length > 1 ? `<button class="ed-btn sm" id="apply-all" type="button" title="Apply every proposal (picture options take the selected or first picture)">Apply all ${props.length}</button>` : `<span class="ed-note">1</span>`}</span></h3>` + props.map((p) => {
      const pending = p.options.filter((o) => !["succeeded", "failed", "gone"].includes(o.status)).length;
      const ready = p.options.length && !pending;
      const fresh = ready && !seenReady.has(p.id); if (ready) seenReady.add(p.id);
      const opts = p.options.map((o, i) => `<button type="button" class="opt ${(pickedOption[p.id] || p.options.find((x) => x.status === "succeeded")?.image_id) === o.image_id ? "on" : ""}" data-opt="${o.image_id}" data-prop="${p.id}" data-n="${i + 1}" data-of="${p.options.length}" data-url="${esc(o.url)}" ${o.status === "succeeded" ? 'title="Click to preview on the stage"' : ""}>${o.status === "succeeded" ? `<img src="${esc(o.url)}" alt="option ${i + 1}">` : o.status === "failed" ? "failed" : `<span>${o.status === "queued" ? "queued" : "drawing"}</span><span class="pg"><i></i></span>`}</button>`).join("");
      const sh = p.shot_id ? shotById(p.shot_id) : null;
      const live = pending ? `<div class="live"><span class="spin"></span>Making ${p.options.length} picture${p.options.length === 1 ? "" : "s"} · ${p.options.length - pending} ready · ${Math.max(0, Math.round(S.now - p.created_at))} s</div>`
        : ready ? `<div class="ed-note">Click an option to preview it on the stage, then use it.</div>` : "";
      return `<div class="ed-prop ${fresh && p.options.length ? "fresh" : ""}" data-p="${p.id}"><div class="h"><span class="s">✦</span><span style="flex:1">${esc(p.title)}</span>${sh ? `<button class="ed-btn ghost sm" data-goto="${sh.id}">${esc(sh.title || "shot " + sh.n)}</button>` : ""}</div>
        ${p.summary ? `<div class="sum">${esc(p.summary)}</div>` : ""}${live}
        ${p.changes.length ? `<ul>${p.changes.map((c) => `<li>${describeChange(c)}</li>`).join("")}</ul>` : ""}
        ${opts ? `<div class="opts">${opts}</div>` : ""}
        <div class="acts three"><button class="ed-btn ghost" data-dismiss="${p.id}" title="Dismiss">✕</button><button class="ed-btn" data-review="${p.id}">Review</button><button class="ed-btn gold" data-applygen="${p.id}" title="Apply, then render the shots it touches">Apply & generate</button></div></div>`;
    }).join("") + "</div>";
  }

  let saveT = null;
  function wireInspector(sh, c, sc) {
    const pane = $("pane-shot");
    pane.querySelectorAll("[data-f]").forEach((el) => {
      el.addEventListener(el.tagName === "SELECT" ? "change" : "input", () => {
        clearTimeout(saveT);
        const go = () => { const v = el.dataset.f === "seconds" ? +el.value : el.value; change([{op: "edit_shot", shot_id: sh.id, [el.dataset.f]: v}]); };
        if (el.tagName === "SELECT") go(); else saveT = setTimeout(go, 900);
      });
      el.addEventListener("blur", () => { if (saveT) { clearTimeout(saveT); saveT = null; const v = el.dataset.f === "seconds" ? +el.value : el.value; if (String(sh[el.dataset.f]) !== String(v)) change([{op: "edit_shot", shot_id: sh.id, [el.dataset.f]: v}]); } });
    });
    pane.querySelectorAll("[data-scene-f]").forEach((el) => el.addEventListener("change", () => change([{op: "edit_scene", scene_id: sc.id, [el.dataset.sceneF]: el.value}], "Saved")));
    bindUseOptions(sh, pane);
    pane.querySelectorAll("[data-make]").forEach((b) => b.onclick = () => change([{op: "edit_shot", shot_id: sh.id, make: b.dataset.make}], `${MAKE[b.dataset.make][0]} shot`));
    pane.querySelectorAll("[data-tref]").forEach((b) => b.onclick = () => { const id = b.dataset.tref; const refs = sh.refs.includes(id) ? sh.refs.filter((x) => x !== id) : [...sh.refs, id]; change([{op: "edit_shot", shot_id: sh.id, refs}]); });
    pane.querySelectorAll("[data-gen]").forEach((b) => b.onclick = () => { genOpen = genOpen === b.dataset.gen ? null : b.dataset.gen; paintInspector(); });
    pane.querySelectorAll("[data-sound]").forEach((b) => b.onclick = async () => {
      const k = b.dataset.sound;
      if (k === "model") {
        if (!confirm("The video model will invent this shot's sound (it won't be the character's own voice). Switch this shot to “Keep characters true to their references” (that model makes sound) and turn its sound on in the mix?")) return;
        await change([{op: "edit_shot", shot_id: sh.id, make: "perform"}, {op: "set_mix", shot: {mute: false}}], "Its own sound is on; render it to hear it");
        genOpen = "video"; paintInspector(); return;
      }
      genOpen = k === "line" ? "voice" : "music"; paintInspector();
      setTimeout(() => { const f = $("pane-shot").querySelector(".ed-genform"); f?.scrollIntoView({block: "center"}); $("g-text")?.focus(); }, 50);
    });
    pane.querySelectorAll("[data-go]").forEach((b) => b.onclick = () => runGen(b.dataset.go, sh, b));
    if ($("ins-upload")) $("ins-upload").onclick = () => uploadPicture(sh.id);
    const rvDone = (r) => { toast("Reference video added" + (r.switched ? " — this shot now uses “Keep characters true to their references”" : "") + ". Remake its video with 🎬."); load(true); };
    if ($("rv-shot")) $("rv-shot").onchange = async () => { const v = $("rv-shot").value; if (!v) return;
      try { rvDone(await api(`/api/editor/${PID}/shots/${sh.id}/ref-videos`, {from_shot: v})); } catch (e) { toast(e.message); } };
    if ($("rv-upload")) $("rv-upload").onclick = () => { const inp = document.createElement("input"); inp.type = "file"; inp.accept = "video/*";
      inp.onchange = async () => { const fd = new FormData(); fd.append("file", inp.files[0]); toast("Uploading the video…");
        try { rvDone(await api(`/api/editor/${PID}/shots/${sh.id}/ref-videos`, fd)); } catch (e) { toast(e.message); } };
      inp.click(); };
    pane.querySelectorAll("[data-rvdel]").forEach((b) => b.onclick = async () => {
      try { await api(`/api/editor/${PID}/shots/${sh.id}/ref-videos/${b.dataset.rvdel}/delete`, {}); toast("Reference video removed"); load(true); } catch (e) { toast(e.message); } });
    if ($("mv-up")) $("mv-up").onclick = () => nudge(sh.id, -1);
    if ($("mv-down")) $("mv-down").onclick = () => nudge(sh.id, 1);
    if ($("mv-scene")) $("mv-scene").onchange = () => { const sc = sceneById($("mv-scene").value); moveShot(sh.id, sc.id, sc.shots.filter((x) => x !== sh.id).length, `Moved to “${sc.title}”`); };
    if ($("ins-ask")) $("ins-ask").onclick = () => { scope = "shot"; paintScope(); setTab("agent"); $("text").focus(); };
    if ($("ins-addref")) $("ins-addref").onclick = (e) => { e.preventDefault(); refModal(null); };
    if ($("sc-add")) $("sc-add").onclick = addShot;
    if ($("sc-more")) $("sc-more").onclick = () => sceneModal(sc);
    if ($("sc-ask")) $("sc-ask").onclick = () => { setTab("agent"); scope = "scene"; paintScope(); chatSend("Define the shots this scene should have."); };
    if ($("ci-close")) $("ci-close").onclick = () => { sel.clip = null; paint(); };
    if ($("ci-voice")) $("ci-voice").onclick = async () => { try { await api(`/api/editor/${PID}/clips/${c.id}/voice`, {}); toast("Voicing the line"); load(true); } catch (e) { toast(e.message); } };
    pane.querySelectorAll("[data-c]").forEach((el) => {
      const k = el.dataset.c;
      el.addEventListener("input", () => { const o = el.nextElementSibling; if (o && o.tagName === "OUTPUT") o.textContent = k === "gain_db" ? `${el.value} dB` : `${(+el.value).toFixed(1)} s`; });
      el.addEventListener("change", () => change([{op: "edit_clip", clip_id: c.id, [k]: el.type === "checkbox" ? el.checked : k === "track" ? el.value : +el.value}],
        k === "use_in_video" ? (el.checked ? "Used for video generation" : "Mix only — not used for video") : "Saved"));
    });
    pane.querySelectorAll("[data-opt]").forEach((b) => b.onclick = () => {
      pickedOption[b.dataset.prop] = b.dataset.opt;
      const p = S.proposals.find((x) => x.id === b.dataset.prop), o = p?.options.find((x) => x.image_id === b.dataset.opt);
      if (o && o.status === "succeeded" && p.shot_id) { if (sel.shot !== p.shot_id) selectShot(p.shot_id); preview = {shot: p.shot_id, prop: p.id, image_id: o.image_id, url: o.url, n: +b.dataset.n, of: +b.dataset.of}; paintStage(); }
      paintInspector(); });
    pane.querySelectorAll("[data-goto]").forEach((b) => b.onclick = () => selectShot(b.dataset.goto));
    pane.querySelectorAll("[data-dismiss]").forEach((b) => b.onclick = async () => { await api(`/api/editor/${PID}/proposals/${b.dataset.dismiss}/dismiss`, {}); load(true); });
    pane.querySelectorAll("[data-review]").forEach((b) => b.onclick = () => openReview(b.dataset.review));
    const apply = async (id, render) => {
      const p = S.proposals.find((x) => x.id === id);
      const opt = pickedOption[id] || p.options.find((o) => o.status === "succeeded")?.image_id || null;
      if (p.options.length && !opt) { toast("The options are still being made"); return; }
      try { const r = await api(`/api/editor/${PID}/proposals/${id}/apply`, {option: opt, render});
        toast((r.rendering ? `Applied — rendering ${r.rendering.length} shot(s)` : "Applied") + (r.failed?.length ? ` · ${r.failed.length} change(s) skipped: ${r.failed[0]}` : "")); load(true); } catch (e) { toast(e.message); load(true); }
    };
    pane.querySelectorAll("[data-apply]").forEach((b) => b.onclick = () => apply(b.dataset.apply, false));
    if ($("apply-all")) $("apply-all").onclick = async () => {
      const ids = S.proposals.map((p) => p.id); let done = 0, failed = [];
      for (const id of ids) {
        const p = S.proposals.find((x) => x.id === id); if (!p) continue;
        const opt = pickedOption[id] || p.options.find((o) => o.status === "succeeded")?.image_id || null;
        if (p.options.length && !opt) { failed.push(p.title + " (pictures not ready)"); continue; }
        try { const r = await api(`/api/editor/${PID}/proposals/${id}/apply`, {option: opt}); done++; if (r.failed) failed.push(...r.failed); } catch (e) { failed.push(p.title + ": " + e.message); }
      }
      toast(`Applied ${done}` + (failed.length ? ` · ${failed.length} skipped` : "")); if (failed.length) console.warn(failed); load(true);
    };
    pane.querySelectorAll("[data-applygen]").forEach((b) => b.onclick = () => apply(b.dataset.applygen, true));
  }

  async function runGen(what, sh, btn) {
    btn.disabled = true;
    try {
      if (what === "edit") { const ins = $("g-edit").value.trim(); if (!ins) { toast("Say what should change"); btn.disabled = false; return; }
        await api(`/api/editor/${PID}/shots/${sh.id}/edit`, {instruction: ins, count: +$("g-ecount").value, model: $("g-emodel").value || null}); toast("Editing the picture — the versions appear at the top of this panel"); setTimeout(() => { $("pane-shot").scrollTop = 0; }, 100); }
      else if (what === "image") { await api(`/api/editor/${PID}/shots/${sh.id}/options`, {prompt: $("g-prompt").value, count: +$("g-count").value, model: $("g-model").value || null}); toast("Making picture options — they appear at the top of this panel"); setTimeout(() => { $("pane-shot").scrollTop = 0; }, 100); }
      else if (what === "video") { await renderShots([sh.id], "this shot"); }
      else if (what === "video-scene") { await renderShots(sceneShots(sh.scene_id).map((s) => s.id), "the scene"); }
      else if (what === "voice") { const text = $("g-text").value.trim(); if (!text) { toast("Write the line"); btn.disabled = false; return; }
        const lip = $("g-lip")?.checked;
        await api(`/api/editor/${PID}/clips`, {kind: "dialogue", ref_id: $("g-ref").value || null, text, instruct: $("g-inst").value, start_s: sh.start + 0.3, use_in_video: lip || $("g-vid").checked, lipsync_shot: lip ? sh.id : null});
        toast(lip ? "Voicing the line first — the video is generated with this voice as soon as it's ready" : "Voicing the line — it lands on the dialogue lane"); }
      else { const d = $("g-text").value.trim(); if (!d) { toast("Describe it"); btn.disabled = false; return; }
        await api(`/api/editor/${PID}/clips`, {kind: what === "music" ? "music" : "ambience", description: d, start_s: sceneSpan(sh.scene_id)[0], duration: +$("g-dur").value, use_in_video: $("g-vid").checked}); toast("Generating… it lands on the lane when ready"); }
      genOpen = null; load(true);
    } catch (e) { toast(e.message); btn.disabled = false; }
  }

  // ------------------------------------------------------------------ assets view
  function paintAssets() {
    const v = $("v-assets");
    if (typing()) return;
    const clips = S.clips;
    v.innerHTML = `<section><div class="ed-sec-h"><h2>Story</h2><span class="ed-note">the agent works from this</span></div><textarea class="ed-story" id="as-story" placeholder="The story. The agent can break it into scenes and shots.">${esc(S.story)}</textarea></section>
      <section><div class="ed-sec-h"><h2>References</h2><button class="ed-btn sm" id="as-addref">＋ Add</button></div><div class="ed-agrid">${S.refs.map((r) => `<div class="ed-acard" data-ref="${r.id}"><div class="th">${r.image ? `<img src="${esc(r.image)}" alt="">` : "👤"}</div><div class="m"><b>${esc(r.name)}</b><span>${esc(r.kind)}${r.voice ? " · 🎙 sample" : r.speaker ? " · 🎙 " + esc(r.speaker) : ""}</span></div></div>`).join("") || `<div class="ed-note">None yet.</div>`}</div></section>
      <section><div class="ed-sec-h"><h2>Shots</h2><span class="ed-note">${S.shots.filter((s) => s.clip).length} clips rendered</span></div><div class="ed-agrid">${S.shots.map((s) => `<div class="ed-acard" data-ashot="${s.id}"><div class="th">${s.picture ? `<img src="${esc(s.picture)}" alt="" loading="lazy">` : "🎞"}</div><div class="m"><b>${esc(s.title || "Shot " + s.n)}</b><span>${esc(sceneById(s.scene_id)?.title || "")} · ${s.seconds}s · ${esc(s.status)}</span></div></div>`).join("")}</div></section>
      <section><div class="ed-sec-h"><h2>Audio</h2></div><div class="ed-agrid">${clips.map((c) => `<div class="ed-acard" data-aclip="${c.id}"><div class="th" style="background:color-mix(in srgb, ${TRACK_COLOR[c.track]} 30%, var(--panel-3))">${{dialogue: "🎙", music: "🎵", ambience: "🌧"}[c.track]}</div><div class="m"><b>${esc(c.name)}</b><span>${TRACK_NAME[c.track]} · ${fmt(c.start_s)} · ${esc(c.status)}${c.use_in_video ? " · 🎬" : ""}</span></div></div>`).join("") || `<div class="ed-note">Click a timeline lane to add dialogue, music or ambience.</div>`}</div></section>`;
    $("as-story").onchange = async () => { await api(`/api/editor/${PID}/settings`, {story: $("as-story").value}); toast("Story saved"); load(true); };
    $("as-addref").onclick = () => refModal(null);
    v.querySelectorAll("[data-ref]").forEach((d) => d.onclick = () => refModal(refById(d.dataset.ref)));
    v.querySelectorAll("[data-ashot]").forEach((d) => d.onclick = () => { setView("storyboard"); selectShot(d.dataset.ashot); });
    v.querySelectorAll("[data-aclip]").forEach((d) => d.onclick = () => { sel.clip = d.dataset.aclip; const c = clipById(sel.clip); const sh = S.shots.find((s) => c.start_s >= s.start && c.start_s < s.start + s.seconds); if (sh) { sel.shot = sh.id; sel.scene = sh.scene_id; } setView("storyboard"); setTab("shot"); });
  }

  // ------------------------------------------------------------------ modal helper
  function modal(html, onAct) {
    $("sheet").innerHTML = html; $("sheet").classList.remove("wide", "review"); $("modal").hidden = false;
    $("sheet").onclick = async (e) => { const b = e.target.closest("[data-act]"); if (!b) return; const done = await onAct(b.dataset.act); if (done !== false) $("modal").hidden = true; };
  }
  $("modal").addEventListener("click", (e) => { if (e.target === $("modal")) $("modal").hidden = true; });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { $("modal").hidden = true; $("pop").hidden = true; ED.classList.remove("left-open"); }
    if (e.target.closest("input, textarea, select")) return;
    if (e.key === " ") { e.preventDefault(); Player.toggle(); }
    if (e.altKey && sel.shot && (e.key === "ArrowRight" || e.key === "ArrowLeft")) { e.preventDefault(); nudge(sel.shot, e.key === "ArrowRight" ? 1 : -1); return; }
    if (e.key === "ArrowRight") stepShot(1); if (e.key === "ArrowLeft") stepShot(-1);
  });

  // ------------------------------------------------------------------ player (live mix preview)
  const Player = {
    playing: false, t: 0, last: 0, range: [0, 0], audios: new Map(), video: null, curShot: null, mode: "",
    rangeNow() { const r = $("prange").value; const sh = shotById(sel.shot);
      if (r === "shot" && sh) return [sh.start, sh.start + sh.seconds];
      if (r === "scene" && sel.scene) return sceneSpan(sel.scene);
      return [0, S.duration]; },
    toggle() { this.playing ? this.stop() : this.play(); },
    play() {
      if (!S || !S.shots.length) return;
      this.range = this.rangeNow();
      if (this.t < this.range[0] || this.t >= this.range[1] - 0.05) this.t = this.range[0];
      this.playing = true; this.last = performance.now(); this.curShot = null; $("play").textContent = "❚❚";
      requestAnimationFrame(() => this.tick());
    },
    stop() {
      this.playing = false; $("play").textContent = "▶";
      this.audios.forEach((a) => a.pause());
      if (this.video) this.video.pause();
      this.curShot = null; this.mode = "";
      if (S && view !== "assets") paintStage();
    },
    seek(t) { this.t = Math.max(0, Math.min(S.duration, t)); const sh = S.shots.find((s) => this.t >= s.start && this.t < s.start + s.seconds);
      if (sh && !this.playing && sh.id !== sel.shot) { sel.shot = sh.id; sel.scene = sh.scene_id; paint(); }
      if (this.playing) { this.audios.forEach((a) => a.pause()); this.curShot = null; } this.paintHead(); },
    paintHead() {
      const h = $("head"); const hx = TL.off + (this.t - TL.a) * TL.pps; if (h) h.style.left = hx + "px";
      const sc = $("tl-scroll"); if (this.playing && sc && (hx > sc.scrollLeft + sc.clientWidth - 40 || hx < sc.scrollLeft + TL.off)) sc.scrollLeft = hx - TL.off - 20;
      const [a, b] = this.playing ? this.range : this.rangeNow();
      $("ptime").textContent = `${fmt(this.t - a)} / ${fmt(b - a)}`;
      $("scrub").value = b > a ? Math.round((this.t - a) / (b - a) * 1000) : 0;
    },
    audio(c) { let el = this.audios.get(c.id); if (!el) { el = new Audio(c.url); el.preload = "auto"; this.audios.set(c.id, el); } return el; },
    tick() {
      if (!this.playing) return;
      const now = performance.now(); this.t += (now - this.last) / 1000; this.last = now;
      if (this.t >= this.range[1]) { this.t = this.range[1]; this.paintHead(); this.stop(); return; }
      const sh = S.shots.find((s) => this.t >= s.start && this.t < s.start + s.seconds);
      if (sh && sh.id !== this.curShot) {
        this.curShot = sh.id;
        if (sh.scene_id !== sel.scene || sh.id !== sel.shot) { sel.shot = sh.id; sel.scene = sh.scene_id; paintCrumbs(); paintStrip(); if (view === "timeline" || true) paintTimeline(); }
        this.mode = sh.clip ? "" : "stage-img"; paintStage(sh);
        this.video = $("stage-v");
        if (this.video) { this.video.currentTime = this.t - sh.start; this.video.muted = sh.make !== "perform" || S.mix.shot.mute; this.video.removeAttribute("controls"); this.video.volume = Math.min(1, amp(S.mix.shot.gain_db)); this.video.play().catch(() => {}); }
      }
      if (this.video && sh && sh.clip && Math.abs(this.video.currentTime - (this.t - sh.start)) > 0.35) this.video.currentTime = this.t - sh.start;
      const dlgOn = S.clips.some((c) => c.track === "dialogue" && c.status === "ready" && this.t >= c.start_s && this.t < c.start_s + (c.duration_s || 0));
      for (const c of S.clips) {
        if (c.status !== "ready" || !c.url) continue;
        const inside = this.t >= c.start_s && this.t < c.start_s + (c.duration_s || 0);
        const el = this.audio(c);
        if (inside) {
          const want = c.trim_s + (this.t - c.start_s);
          let g = c.gain_db + S.mix[c.track].gain_db;
          if (dlgOn && c.track !== "dialogue" && S.mix.duck_db < -0.5) g += S.mix.duck_db;
          let v = Math.min(1, amp(g));
          const into = this.t - c.start_s, left = c.start_s + c.duration_s - this.t;
          if (c.fade_in > 0 && into < c.fade_in) v *= into / c.fade_in;
          if (c.fade_out > 0 && left < c.fade_out) v *= left / c.fade_out;
          el.volume = S.mix[c.track].mute ? 0 : Math.max(0, v);
          if (el.paused) { el.currentTime = want; el.play().catch(() => {}); }
          else if (Math.abs(el.currentTime - want) > 0.3) el.currentTime = want;
        } else if (!el.paused) el.pause();
      }
      this.paintHead();
      requestAnimationFrame(() => this.tick());
    },
  };
  $("play").onclick = () => Player.toggle();
  $("scrub").oninput = () => { const [a, b] = Player.playing ? Player.range : Player.rangeNow(); Player.seek(a + (b - a) * $("scrub").value / 1000); };
  $("prange").onchange = () => { Player.stop(); Player.t = Player.rangeNow()[0]; Player.paintHead(); };

  // Waiting proposals, shown where the conversation is, with the buttons right there.
  function paintPropbar() {
    const bar = $("propbar"), props = S.proposals;
    bar.hidden = !props.length;
    if (!props.length) { bar.innerHTML = ""; return; }
    const what = (p) => p.options.length ? `${p.options.length} picture option(s)` : (() => {
      const c = p.changes, k = (op) => c.filter((x) => x.op === op).length;
      return [k("add_scene") + k("edit_scene") && `${k("add_scene") + k("edit_scene")} scene(s)`, k("add_shot") && `${k("add_shot")} shot(s)`,
        k("add_ref") && `${k("add_ref")} reference(s)`].filter(Boolean).join(" · ") || `${c.length} change(s)`; })();
    bar.innerHTML = `<div class="h">✨ Waiting for you <span class="sp"></span>${props.length > 1 ? `<button class="ed-btn sm gold" data-pb-all>Apply all ${props.length}</button>` : ""}</div>`
      + props.map((p) => `<div class="row"><div><b>${esc(p.title)}</b><span>${esc(what(p))}</span></div>
        <button class="ed-btn sm" data-pb-review="${p.id}">Review</button><button class="ed-btn sm gold" data-pb-apply="${p.id}">Apply</button></div>`).join("");
  }
  $("propbar").addEventListener("click", async (e) => {
    const rv = e.target.closest("[data-pb-review]"), ap = e.target.closest("[data-pb-apply]"), all = e.target.closest("[data-pb-all]");
    if (rv) { openReview(rv.dataset.pbReview); return; }
    const ids = all ? S.proposals.map((p) => p.id) : ap ? [ap.dataset.pbApply] : [];
    if (!ids.length) return;
    e.target.disabled = true; let done = 0; const failed = [];
    for (const id of ids) {
      const p = S.proposals.find((x) => x.id === id); if (!p) continue;
      const opt = pickedOption[id] || p.options.find((o) => o.status === "succeeded")?.image_id || null;
      if (p.options.length && !opt) { failed.push(p.title + " (pictures not ready)"); continue; }
      try { const r = await api(`/api/editor/${PID}/proposals/${id}/apply`, {option: opt}); done++; failed.push(...(r.failed || [])); } catch (err) { failed.push(err.message); }
    }
    toast(`Applied ${done}` + (failed.length ? ` · ${failed.length} skipped: ${failed[0]}` : "")); load(true);
  });

  // ------------------------------------------------------------------ agent
  function focusNow() {
    if (scope === "story") return {level: "story", label: "the whole story"};
    const sc = sceneById(sel.scene);
    if (scope === "scene") return sc ? {level: "scene", scene_id: sc.id, label: `“${sc.title}”`} : {level: "story"};
    const sh = shotById(sel.shot); if (!sh) return sc ? {level: "scene", scene_id: sc.id, label: `“${sc.title}”`} : {level: "story"};
    return {level: "shot", shot_id: sh.id, scene_id: sh.scene_id, label: `${sc ? sc.title + " › " : ""}Shot ${String(sceneShots(sh.scene_id).indexOf(sh) + 1).padStart(2, "0")}${sh.title ? " · " + sh.title : ""}`};
  }
  function paintScope() {
    ["story", "scene", "shot"].forEach((k) => $("scope-" + k).classList.toggle("on", scope === k));
    const quick = scope === "shot"
      ? ["Define this shot: framing, length, prompt, references", "Give me 2 picture options for this shot", "Write a line for this shot and voice it", "Render this shot now"]
      : scope === "scene"
      ? ["Define the shots this scene should have", "Make pictures for this scene's characters and places", "Give every shot in this scene a picture", "Score this scene", "Render this scene"]
      : ["Define all the scenes and their shots", "Understand the story and summarise it", "Make pictures for every character and place", "Score each scene and duck the music under dialogue", "Render every shot that isn't rendered"];
    $("quick").innerHTML = quick.map((q) => `<button type="button">${esc(q)}</button>`).join("");
    if ($("quick-empty")) $("quick-empty").innerHTML = $("quick").innerHTML;
    paintInspectorHead();
  }
  ["story", "scene", "shot"].forEach((k) => $("scope-" + k).onclick = () => { scope = k; paintScope(); });
  ["quick", "quick-empty"].forEach((id) => $(id)?.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) chatSend(b.textContent); }));
  $("applies").onchange = async () => { await api(`/api/editor/${PID}/settings`, {agent_applies: $("applies").checked}); toast($("applies").checked ? "The agent applies its edits directly" : "The agent proposes; you apply"); };

  let threadId = null, lastAgentStatus = null, threadP = null, chatP = null;
  // Both are memoised as promises: the page load, opening the Agent tab and sending can all
  // ask at once, and three chat panels on one thread drew every message three times.
  function ensureThread() {
    if (threadId) return Promise.resolve(threadId);
    return threadP ||= api(`/api/editor/${PID}/agent`, {}).then((d) => (threadId = d.id)).finally(() => { threadP = null; });
  }
  function ensureChat() {
    return chatP ||= ensureThread().then(() => (chat = A.mount({root: $("chatroot"), threadId, focus: focusNow, ensureThread,
      onUpdate: (v) => { if (lastAgentStatus && lastAgentStatus !== v.status) load(true); else if (["executing", "watching"].includes(v.status)) load(); lastAgentStatus = v.status; }})));
  }
  async function chatSend(text) { setTab("agent"); const c = await ensureChat(); c.send(text); }

  // ------------------------------------------------------------------ start
  // Everything above <main> (the header AND the files-are-deleted note), not just the header:
  // counting only the header made the editor taller than the screen.
  const setHdr = () => document.documentElement.style.setProperty("--hdr", Math.round(document.querySelector("main")?.getBoundingClientRect().top + scrollY || 50) + "px");
  setHdr(); addEventListener("resize", setHdr);
  paintScope();
  load(true);
  // If the agent has been working here before, keep it live in the background so
  // the editor refreshes when it finishes something.
  api(`/api/editor/${PID}`).then((st) => { if (st.thread_id) { threadId = st.thread_id; ensureChat(); } });
})();
