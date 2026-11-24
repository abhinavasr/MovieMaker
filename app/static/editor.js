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
  const MAKE = {animate: ["Animate", "The picture comes to life. Silent; sound comes from the timeline."],
    perform: ["Performance", "Reference pictures + the audio under the shot marked “use in video” → a clip with its own sound (MiniMax ref2v)."],
    lipsync: ["Lip-sync", "The picture speaks or sings the dialogue line under the shot, mouth in sync (Wan2.2-S2V)."]};
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
      const s2 = JSON.stringify({...st, now: 0, deleted_on: 0, days_left: 0});
      const changed = s2 !== sig;
      S = st; sig = s2;
      if (!sel.scene || !sceneById(sel.scene)) sel.scene = S.scenes[0]?.id || null;
      if (sel.shot && !shotById(sel.shot)) sel.shot = null;
      if (!sel.shot) sel.shot = sceneShots(sel.scene)[0]?.id || null;
      if (sel.clip && !clipById(sel.clip)) sel.clip = null;
      if (changed || force) paint();
      else { paintRender(); if (S.proposals.some((p) => p.options.some((o) => !["succeeded", "failed", "gone"].includes(o.status)))) { if (!Player.playing && view !== "assets") paintStage(); if (!typing()) paintInspector(); } }
    } catch (e) { console.warn(e); }
    const busy = S && (["running", "queued"].includes(S.status) || S.clips.some((c) => c.status === "pending") || S.export.status === "running" || S.proposals.some((p) => p.options.some((o) => !["succeeded", "failed", "gone"].includes(o.status))));
    timer = setTimeout(load, busy ? 2000 : 6000);
  }

  function paint() {
    if (document.activeElement !== $("ptitle")) $("ptitle").value = S.title;
    if (document.activeElement !== $("aspect")) $("aspect").value = S.aspect;
    paintStory(); paintScenes(); paintRefs(); paintRender();
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
      box.innerHTML = `<span class="bar"><i style="width:${S.progress}%"></i></span><span title="${esc(S.message)}">${esc(S.status === "queued" ? "waiting for the GPU" : S.message || "rendering")}</span><button class="ed-btn sm ghost" id="cancel-render" type="button">Cancel</button>`;
      $("cancel-render").onclick = async () => { await api(`/api/editor/${PID}/cancel`, {}); toast("Cancelling"); load(true); };
    } else box.innerHTML = S.shots.length ? `<span>${done}/${S.shots.length} shots rendered</span>` : "";
    $("render-missing").disabled = !S.shots.length || ["running", "queued"].includes(S.status) || done === S.shots.length;
    const ex = S.export;
    const b = $("export");
    if (ex.status === "running") { b.textContent = "⏳ Mixing…"; b.disabled = true; }
    else { b.disabled = false; b.textContent = ex.url ? "⤓ Export (ready)" : "⤓ Export"; }
  }
  $("render-missing").onclick = async () => { try { await api(`/api/editor/${PID}/render`, {}); toast("Rendering the missing shots"); load(true); } catch (e) { toast(e.message); } };
  $("export").onclick = () => openExport();
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
        ${missing.length ? `<button class="ed-btn" data-act="render" title="Uses the video model">▶ Render ${missing.length} missing first</button>` : ""}
        <button class="ed-btn gold" data-act="mix">${ex.url ? "Mix again" : "Mix the film"}</button>
        <button class="ed-btn ghost" data-act="close">Close</button></div>`, async (act) => {
      if (act === "render") { try { await api(`/api/editor/${PID}/render`, {}); toast("Rendering the missing shots"); } catch (e) { toast(e.message); } return true; }
      if (act === "mix") { try { await api(`/api/editor/${PID}/export`, {}); toast("Mixing the film…"); load(true); } catch (e) { toast(e.message); } return true; }
      return true;
    });
  }

  // ------------------------------------------------------------------ left: story
  function paintStory() {
    const tx = (S.story || "").replace(/[#*_`>]/g, "").trim();
    $("story-card").innerHTML = (tx ? `<div class="tx">${esc(tx.slice(0, 600))}</div>` : `<div>No story yet. Write or paste it here, or develop it with the agent.</div>`)
      + `<div class="acts"><button class="ed-btn sm" id="story-build" type="button">✨ Build scenes & shots from the story</button></div>`;
    $("story-build").onclick = (e) => { e.stopPropagation(); if (!tx) { storyModal(); return; }
      scope = "story"; paintScope(); setTab("agent");
      chatSend("Define all the scenes and their shots."); };
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
    const d = e.target.closest("[data-scene]"); if (!d) return; selectScene(d.dataset.scene); ED.classList.remove("left-open"); });
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
      <div><b>${esc(r.name)}</b><span>${esc(r.kind[0].toUpperCase() + r.kind.slice(1))}${r.description ? " · " + esc(r.description) : ""}</span>${r.voice ? `<span class="v">🎙 voice sample</span>` : r.speaker ? `<span class="v">🎙 ${esc(r.speaker)}</span>` : ""}</div>
      <span>⋮</span></div>`).join("") : `<div class="ed-note" style="padding:4px 6px">Characters, places and voices the film keeps coming back to. Shots that include them get them as references, so faces and voices stay consistent.</div>`;
  }
  $("refs").addEventListener("click", (e) => { const d = e.target.closest("[data-ref]"); if (d) refModal(refById(d.dataset.ref)); });
  $("add-ref").onclick = () => refModal(null);
  $("refs-gen").onclick = async () => {
    const missing = S.refs.filter((r) => !r.image && !r.image_pending).length;
    if (!missing) { toast("Every reference already has a picture"); return; }
    $("refs-gen").disabled = true; toast(`Drawing ${missing} reference picture(s)…`);
    try { await api(`/api/editor/${PID}/ref-pictures`, {}); load(true); } catch (e) { toast(e.message); }
    finally { $("refs-gen").disabled = false; }
  };
  $("open-left").onclick = () => ED.classList.toggle("left-open");

  function refModal(r) {
    const kinds = ["character", "location", "prop", "style", "voice"];
    let pickedImage = null, polling = null;
    const voiced = (k) => k === "character" || k === "voice";
    modal(`<h2>${r ? "Edit reference" : "New reference"}</h2>
      <div class="ed-row2"><label class="ed-field">Name<input id="rf-name" value="${esc(r?.name || "")}" maxlength="80" placeholder="Name"></label>
      <label class="ed-field">Kind<select id="rf-kind">${kinds.map((k) => `<option ${r?.kind === k ? "selected" : ""}>${k}</option>`).join("")}</select></label></div>
      <label class="ed-field">Description (used in prompts)<textarea id="rf-desc" rows="3" placeholder="How they look — used in every prompt they appear in">${esc(r?.description || "")}</textarea></label>
      <div class="ed-field">Picture
        <div class="ed-refpic">
          <div class="ed-drop" id="rf-pic">${r?.image ? `<img src="${esc(r.image)}" alt="">` : r?.image_pending ? "⏳ being drawn…" : ""}Click or drop a picture</div>
          <div class="ed-refgen">
            <button class="ed-btn gold" type="button" id="rf-gen">✨ Generate picture</button>
            <label class="ed-note">Options <select id="rf-count"><option>1</option><option selected>2</option><option>3</option><option>4</option></select></label>
            <details><summary class="ed-note">Edit the prompt</summary><textarea id="rf-prompt" rows="3" placeholder="Empty = from the name, kind and description, in the film's style"></textarea></details>
          </div>
        </div>
        <div class="ed-refopts" id="rf-opts"></div></div>
      <input type="file" id="rf-picfile" accept="image/*" hidden>
      <div class="ed-row2" id="rf-voicerow" ${voiced(r?.kind || "character") ? "" : "hidden"}><label class="ed-field">Preset voice<select id="rf-speaker"><option value="">— none —</option>${S.speakers.map((s) => `<option ${r?.speaker === s ? "selected" : ""}>${esc(s)}</option>`).join("")}</select></label>
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
      if ($("rf-picfile").files[0]) fd.append("picture", $("rf-picfile").files[0]);
      else if (pickedImage) fd.append("image_id", pickedImage);
      if (voiced($("rf-kind").value) && $("rf-voicefile").files[0]) fd.append("voice", $("rf-voicefile").files[0]);
      if (!fd.get("name")) throw new Error("Give it a name");
      const d = await api(`/api/editor/${PID}/refs${r ? "/" + r.id : ""}`, fd);
      if (!r && d.id) r = {id: d.id, kind: fd.get("kind"), name: fd.get("name")};
      return r;
    }
    $("rf-kind").onchange = () => { $("rf-voicerow").hidden = !voiced($("rf-kind").value); };
    $("rf-gen").onclick = async () => {
      const btn = $("rf-gen"); btn.disabled = true;
      try {
        await saveRef();                         // a new reference is created first; edits are kept
        const d = await api(`/api/editor/${PID}/refs/${r.id}/generate`, {prompt: $("rf-prompt").value, count: +$("rf-count").value});
        if (!$("rf-prompt").value) $("rf-prompt").placeholder = d.prompt;
        const ids = d.image_ids;
        $("rf-opts").innerHTML = ids.map((id, i) => `<button type="button" class="opt" data-img="${id}"><span>drawing ${i + 1}…</span><span class="pg"><i></i></span></button>`).join("");
        const t0 = Date.now();
        const poll = async () => {
          if ($("modal").hidden || !$("rf-opts")) return;
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
    $("rf-opts").addEventListener("click", async (e) => {
      const b = e.target.closest("[data-img]"); if (!b || !b.querySelector("img")) return;
      pickedImage = b.dataset.img; $("rf-picfile").value = "";
      $("rf-opts").querySelectorAll(".opt").forEach((x) => x.classList.toggle("on", x === b));
      $("rf-pic").innerHTML = `<img src="${esc(b.querySelector("img").src)}" alt="">Chosen — Save to keep it`;
    });
    const hook = (drop, input, show) => { $(drop).onclick = () => $(input).click(); $(input).onchange = () => show($(input).files[0]);
      $(drop).ondragover = (e) => e.preventDefault(); $(drop).ondrop = (e) => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (!f) return; const dt = new DataTransfer(); dt.items.add(f); $(input).files = dt.files; show(f); }; };
    hook("rf-pic", "rf-picfile", (f) => { pickedImage = null; $("rf-pic").innerHTML = `<img src="${URL.createObjectURL(f)}" alt="">${esc(f.name)}`; });
    hook("rf-voice", "rf-voicefile", (f) => { $("rf-voice").textContent = "🎙 " + f.name; });
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
    const mk = `<span class="ed-badge">${{animate: "🎞", perform: "🎭", lipsync: "👄"}[sh.make]} ${MAKE[sh.make][0]}${sh.framing ? " · " + sh.framing : ""} · ${sh.seconds}s</span>`;
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
      st.innerHTML = `<div class="ed-frame" id="frame"><img id="stage-i" src="${esc(sh.picture)}" alt="" class="${Player.playing ? "kb" : ""}"></div>${mk}<span class="ed-badge r">${esc(state)}</span>`
        + (snd.tracks.length || sh.make !== "animate" ? (snd.tracks.length ? `<span class="ed-sound">🔊 ${esc(snd.tracks.join(" · "))}</span>` : "")
           : `<span class="ed-sound none">🔇 No sound in this shot <button type="button" id="give-sound">Give it sound</button></span>`);
      fitFrame();
      if ($("give-sound")) $("give-sound").onclick = () => { setTab("shot"); $("pane-shot").scrollTop = 0; };
    } else if (sh.start_from === "prev_last") {
      const prev = S.shots[S.shots.findIndex((x) => x.id === sh.id) - 1];
      st.innerHTML = `<div class="empty"><b>⛓ Opens on the last frame of the previous shot</b><span>Taken from “${esc(prev?.title || "the previous shot")}” when this shot renders, so the two flow as one take.</span>
        <div class="row"><button class="ed-btn gold" id="stage-chain-render">▶ Render both</button></div></div>${mk}`;
      $("stage-chain-render").onclick = async () => { try { await api(`/api/editor/${PID}/render`, {shot_ids: [prev?.id, sh.id].filter(Boolean)}); toast("Rendering both shots"); load(true); } catch (e) { toast(e.message); } };
    } else if (making) {
      const n = making.options.length, done = making.options.filter((o) => o.status === "succeeded").length;
      st.innerHTML = `<div class="gen"><span class="spin"></span><b>Making ${n} picture option${n === 1 ? "" : "s"}…</b><span class="ed-note">${done} of ${n} ready · ${Math.max(0, Math.round(S.now - making.created_at))} s · they appear in the panel on the right</span></div>${mk}`;
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
      <div class="th">${s.picture ? `<img src="${esc(s.picture)}" alt="" loading="lazy">` : s.start_from === "prev_last" ? "⛓ continues" : "no picture"}</div>
      <span class="mk">${{animate: "🎞", perform: "🎭", lipsync: "👄"}[s.make]} ${s.seconds}s</span>
      ${(() => { const b = {done: "● clip", running: "⏳ rendering", failed: "failed", stale: "changed"}[s.status] || (s.status === "pending" && ["running", "queued"].includes(S.status) ? "queued" : ""); return b ? `<span class="st ${esc(s.status)}">${b}</span>` : ""; })()}
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
    $("tl-time").textContent = `${fmt(a)} – ${fmt(b)}`;
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
      return `<div class="ed-blk ${track} ${c.status} ${c.id === sel.clip ? "sel" : ""}" data-clip="${c.id}" style="left:${x(c.start_s)}px;width:${Math.max(8, d * TL.pps)}px" title="${esc(c.name)}${c.status === "failed" ? " — " + esc(c.error || "failed") : ""}">
        <span>${c.status === "pending" ? "⏳ " : ""}${esc(c.name)}</span>${c.use_in_video ? `<span class="vid">🎬</span>` : ""}<span class="rs"></span></div>`;
    }).join("");
    const shotSound = shotsIn.filter((s) => s.make !== "animate").map((s) => `<div class="ed-blk shot ${s.id === sel.shot ? "on" : ""}" style="left:${x(s.start)}px;width:${s.seconds * TL.pps}px;opacity:.75" title="${s.make === "lipsync" ? "Lip-sync: the line is on the dialogue track, so this isn't mixed twice" : "Performance shot's own sound"}">${s.make === "lipsync" ? "👄 in dialogue" : "🎭 " + esc(s.title || "shot")}</div>`).join("");
    const keep = [$("tl-scroll").scrollLeft, $("tl-scroll").scrollTop];
    lanesEl.innerHTML = `<div></div><div class="ed-lane ruler" id="ruler">${ticks}</div>
      <div class="ed-lh"><span class="nm">🎞 Shots</span></div><div class="ed-lane shots">${shotsIn.map((s) => `<div class="ed-blk shot ${s.id === sel.shot ? "on" : ""} ${s.scene_id !== sel.scene && view !== "timeline" ? "other" : ""}" data-tshot="${s.id}" title="Drag to move this shot" style="cursor:grab;left:${x(s.start)}px;width:${s.seconds * TL.pps - 2}px">${esc(s.framing || "")} ${esc(s.title || "shot " + s.n)}</div>`).join("")}</div>
      ${["dialogue", "ambience", "music"].map((t) => lh(t) + `<div class="ed-lane" data-lane="${t}">${blocks(t)}</div>`).join("")}
      ${S.shots.some((s) => s.make !== "animate") ? lh("shot") + `<div class="ed-lane" style="cursor:default">${shotSound}</div>` : ""}
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
  function setTab(t) {
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

  function shotInspector(sh) {
    const cap = S.limits[sh.make];
    const refChips = S.refs.map((r) => `<div class="ed-chip ${sh.refs.includes(r.id) ? "" : "off"}" data-tref="${r.id}" title="${sh.refs.includes(r.id) ? "In this shot — click to remove" : "Click to add to this shot"}">
      <div class="th">${r.image ? `<img src="${esc(r.image)}" alt="">` : ({voice: "🎙", location: "🏛"}[r.kind] || "👤")}</div><span>${esc(r.name)}</span><button aria-label="toggle">${sh.refs.includes(r.id) ? "✕" : "＋"}</button></div>`).join("");
    const lines = S.clips.filter((c) => c.track === "dialogue" && c.start_s < sh.start + sh.seconds && c.start_s + (c.duration_s || 0) > sh.start);
    const gen = (key, icon, label, small) => `<button type="button" data-gen="${key}"><span class="i">${icon}</span><span>${label}</span><span class="a">${genOpen === key ? "⌄" : "›"}</span>${small ? `<small>${small}</small>` : ""}</button>` + (genOpen === key ? genForm(key, sh) : "");
    const snd = soundUnder(sh);
    const silent = !snd.tracks.length && sh.make === "animate";
    const silentCard = silent ? `<div class="ed-silent"><div class="h">🔇 This shot has no sound</div>
      <div class="ed-note">Animate shots make picture only. Sound comes from the timeline, or from a shot that makes its own.</div>
      <div class="opts">
        <button type="button" data-sound="line"><span>🎙</span>Speak a line<small>voiced on the dialogue track and mixed in — no re-render</small></button>
        <button type="button" data-sound="perform"><span>🎭</span>Let it make its own sound<small>Performance: the video model generates sound with the picture</small></button>
        <button type="button" data-sound="music"><span>🎵</span>Music or ambience<small>score or a soundscape under this scene</small></button>
      </div></div>` : "";
    const inScene = sceneShots(sh.scene_id), idx = inScene.findIndex((x) => x.id === sh.id);
    const moveRow = `<div class="ed-move"><label class="ed-field">Scene<select id="mv-scene">${S.scenes.map((sc, i) => `<option value="${sc.id}" ${sc.id === sh.scene_id ? "selected" : ""}>${i + 1}. ${esc(sc.title || "Untitled scene")}</option>`).join("")}</select></label>
      <button class="ed-btn icon" id="mv-up" type="button" title="Move earlier (Alt+←)" aria-label="Move earlier">◀</button><span class="ed-note" style="padding-bottom:9px;white-space:nowrap">${idx + 1} of ${inScene.length}</span><button class="ed-btn icon" id="mv-down" type="button" title="Move later (Alt+→)" aria-label="Move later">▶</button></div>`;
    return silentCard + `<div class="ed-group">
      <label class="ed-field">Title<input data-f="title" value="${esc(sh.title)}" maxlength="80" placeholder="Name this shot"></label></div>${moveRow}
      <div class="ed-row2">
        <label class="ed-field">Framing<select data-f="framing">${S.framings.map((f) => `<option value="${f}" ${f === sh.framing ? "selected" : ""}>${esc(f ? f + " · " + FRAMING[f] : "—")}</option>`).join("")}</select></label>
        <label class="ed-field">Seconds (max ${cap})<input data-f="seconds" type="number" min="1" max="${cap}" value="${sh.seconds}"></label></div>
      <label class="ed-field">Opening picture<select data-f="start_from">
        <option value="" ${sh.start_from ? "" : "selected"}>Its own picture</option>
        <option value="prev_last" ${sh.start_from === "prev_last" ? "selected" : ""} ${S.shots[0]?.id === sh.id ? "disabled" : ""}>Last frame of the previous shot (continuous)</option></select></label>
      <div class="ed-group"><div class="ed-seg" role="radiogroup" aria-label="How the clip is made">${Object.keys(MAKE).map((m) => `<button type="button" data-make="${m}" class="${sh.make === m ? "on" : ""}">${{animate: "🎞", perform: "🎭", lipsync: "👄"}[m]} ${MAKE[m][0]}</button>`).join("")}</div>
        <div class="ed-note" style="margin-top:5px">${MAKE[sh.make][1]}${sh.make !== "animate" ? (lines.length ? ` Under it: ${lines.map((l) => esc(l.name)).join("; ")}${lines.some((l) => !l.use_in_video) ? " (some not marked “use in video”)" : ""}.` : " <b style='color:var(--warn)'>No dialogue under this shot yet</b> — click the Dialogue lane under it.") : ""}</div></div>
      <label class="ed-field">Prompt — what moves, how the camera moves<textarea data-f="prompt" rows="4" placeholder="What moves, and how the camera moves…">${esc(sh.prompt)}</textarea></label>
      <div class="ed-group"><h3>References <span class="c ed-note">${sh.refs.length} in this shot</span></h3>
        ${S.refs.length ? `<div class="ed-chips">${refChips}</div>` : `<div class="ed-note">No references yet. <a href="#" id="ins-addref" style="color:var(--gold)">Add a character or place</a> so it stays consistent across shots.</div>`}</div>
      <div class="ed-group"><h3>Generate</h3><div class="ed-gen">
        ${gen("image", "🖼", "Image", sh.picture ? "new picture options (keeps the references)" : "this shot has no picture yet")}
        ${gen("video", "🎬", "Video", sh.status === "done" ? "clip ready — render again" : sh.status === "stale" ? "changed since rendered" : "render this shot's clip")}
        ${gen("voice", "🎙", "Voice", "a line of dialogue at this shot")}
        ${gen("music", "🎵", "Music", "score for this scene")}
        ${gen("sfx", "🌧", "SFX / ambience", "a soundscape under this scene")}
      </div></div>
      <div class="ed-group" style="display:flex;gap:6px;flex-wrap:wrap"><button class="ed-btn sm" id="ins-upload">⤒ Upload picture</button>${sh.clip ? `<a class="ed-btn sm" href="${esc(sh.clip)}" download>⤓ Clip</a>` : ""}<button class="ed-btn sm" id="ins-ask">✨ Ask about this shot</button></div>`;
  }

  function genForm(key, sh) {
    if (key === "image") return `<div class="ed-genform"><label class="ed-field">Picture prompt (empty = from the shot's framing, title, prompt and scene)<textarea id="g-prompt" rows="3"></textarea></label>
      <div class="ed-row2"><label class="ed-field">Options<select id="g-count"><option>1</option><option selected>2</option><option>3</option><option>4</option></select></label>
      <label class="ed-field">Model<select id="g-model"><option value="">FLUX.2 klein (default)</option><option value="qwen-image-2.1-turbo">Qwen-Image turbo</option><option value="qwen-image-2.1">Qwen-Image</option></select></label></div>
      <div class="acts"><button class="ed-btn gold" data-go="image">Generate options</button></div><div class="ed-note">Uses this shot's references as source pictures. You pick one under Proposed changes.</div></div>`;
    if (key === "video") return `<div class="ed-genform"><div class="ed-note">${MAKE[sh.make][0]} · ${sh.seconds} s · about ${Math.ceil(sh.seconds / 5 * 2)} min of GPU. ${!sh.picture && sh.start_from !== "prev_last" ? "<b style='color:var(--warn)'>Needs a picture first.</b>" : sh.start_from === "prev_last" ? "Opens on the previous shot's last frame." : ""} ${!sh.prompt ? "<b style='color:var(--warn)'>Needs a prompt.</b>" : ""}</div>
      <div class="acts"><button class="ed-btn" data-go="video-scene">Render whole scene</button><button class="ed-btn gold" data-go="video">Render this shot</button></div></div>`;
    if (key === "voice") { const sp = S.refs.filter((r) => ["character", "voice"].includes(r.kind) && sh.refs.includes(r.id)).concat(S.refs.filter((r) => ["character", "voice"].includes(r.kind) && !sh.refs.includes(r.id)));
      return `<div class="ed-genform"><label class="ed-field">Who<select id="g-ref">${sp.map((r) => `<option value="${r.id}">${esc(r.name)}</option>`).join("")}<option value="">Narrator / default voice</option></select></label>
      <label class="ed-field">Line<textarea id="g-text" rows="3"></textarea></label><label class="ed-field">Delivery<input id="g-inst" placeholder="trembling, quiet"></label>
      <label class="ed-switch"><input type="checkbox" id="g-vid" checked> Use in video</label>
      ${sh.picture || sh.start_from === "prev_last" ? `<label class="ed-switch"><input type="checkbox" id="g-lip"> Also regenerate this shot lip-synced to the line (uses the video model; otherwise the line is just mixed in)</label>` : ""}
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
      ${c.status === "pending" ? `<div class="ed-note">⏳ Generating…</div>` : c.status === "failed" ? `<div class="ed-note" style="color:var(--bad)">${esc(c.error || "failed")}</div>` : c.url ? `<audio src="${esc(c.url)}" controls style="width:100%"></audio>` : ""}
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
        <div class="acts three"><button class="ed-btn ghost" data-dismiss="${p.id}" title="Dismiss">✕</button><button class="ed-btn" data-apply="${p.id}">Apply</button><button class="ed-btn gold" data-applygen="${p.id}" title="Apply, then render the shots it touches">Apply & generate</button></div></div>`;
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
    pane.querySelectorAll("[data-make]").forEach((b) => b.onclick = () => change([{op: "edit_shot", shot_id: sh.id, make: b.dataset.make}], `${MAKE[b.dataset.make][0]} shot`));
    pane.querySelectorAll("[data-tref]").forEach((b) => b.onclick = () => { const id = b.dataset.tref; const refs = sh.refs.includes(id) ? sh.refs.filter((x) => x !== id) : [...sh.refs, id]; change([{op: "edit_shot", shot_id: sh.id, refs}]); });
    pane.querySelectorAll("[data-gen]").forEach((b) => b.onclick = () => { genOpen = genOpen === b.dataset.gen ? null : b.dataset.gen; paintInspector(); });
    pane.querySelectorAll("[data-sound]").forEach((b) => b.onclick = async () => {
      const k = b.dataset.sound;
      if (k === "perform") { await change([{op: "edit_shot", shot_id: sh.id, make: "perform"}], "Performance shot — render it to hear its sound"); genOpen = "video"; paintInspector(); return; }
      genOpen = k === "line" ? "voice" : "music"; paintInspector();
      setTimeout(() => { const f = $("pane-shot").querySelector(".ed-genform"); f?.scrollIntoView({block: "center"}); $("g-text")?.focus(); }, 50);
    });
    pane.querySelectorAll("[data-go]").forEach((b) => b.onclick = () => runGen(b.dataset.go, sh, b));
    if ($("ins-upload")) $("ins-upload").onclick = () => uploadPicture(sh.id);
    if ($("mv-up")) $("mv-up").onclick = () => nudge(sh.id, -1);
    if ($("mv-down")) $("mv-down").onclick = () => nudge(sh.id, 1);
    if ($("mv-scene")) $("mv-scene").onchange = () => { const sc = sceneById($("mv-scene").value); moveShot(sh.id, sc.id, sc.shots.filter((x) => x !== sh.id).length, `Moved to “${sc.title}”`); };
    if ($("ins-ask")) $("ins-ask").onclick = () => { scope = "shot"; paintScope(); setTab("agent"); $("text").focus(); };
    if ($("ins-addref")) $("ins-addref").onclick = (e) => { e.preventDefault(); refModal(null); };
    if ($("sc-add")) $("sc-add").onclick = addShot;
    if ($("sc-more")) $("sc-more").onclick = () => sceneModal(sc);
    if ($("sc-ask")) $("sc-ask").onclick = () => { setTab("agent"); scope = "scene"; paintScope(); chatSend("Define the shots this scene should have."); };
    if ($("ci-close")) $("ci-close").onclick = () => { sel.clip = null; paint(); };
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
      if (what === "image") { await api(`/api/editor/${PID}/shots/${sh.id}/options`, {prompt: $("g-prompt").value, count: +$("g-count").value, model: $("g-model").value || null}); toast("Making picture options — they appear at the top of this panel"); setTimeout(() => { $("pane-shot").scrollTop = 0; }, 100); }
      else if (what === "video") { await api(`/api/editor/${PID}/render`, {shot_ids: [sh.id]}); toast("Rendering this shot"); }
      else if (what === "video-scene") { await api(`/api/editor/${PID}/render`, {shot_ids: sceneShots(sh.scene_id).map((s) => s.id)}); toast("Rendering the scene"); }
      else if (what === "voice") { const text = $("g-text").value.trim(); if (!text) { toast("Write the line"); btn.disabled = false; return; }
        const lip = $("g-lip")?.checked;
        await api(`/api/editor/${PID}/clips`, {kind: "dialogue", ref_id: $("g-ref").value || null, text, instruct: $("g-inst").value, start_s: sh.start + 0.3, use_in_video: lip || $("g-vid").checked, lipsync_shot: lip ? sh.id : null});
        toast(lip ? "Speaking the line — the shot regenerates lip-synced when the voice is ready" : "Speaking… it lands on the dialogue lane and is mixed in"); }
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
    $("sheet").innerHTML = html; $("sheet").classList.remove("wide"); $("modal").hidden = false;
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
    if (rv) { setTab("shot"); $("pane-shot").scrollTop = 0; document.querySelector(`.ed-prop[data-p="${rv.dataset.pbReview}"]`)?.scrollIntoView({block: "start"}); return; }
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
    $("quick-empty").innerHTML = $("quick").innerHTML;
    paintInspectorHead();
  }
  ["story", "scene", "shot"].forEach((k) => $("scope-" + k).onclick = () => { scope = k; paintScope(); });
  ["quick", "quick-empty"].forEach((id) => $(id).addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) chatSend(b.textContent); }));
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
  const setHdr = () => document.documentElement.style.setProperty("--hdr", (document.querySelector("header.top")?.offsetHeight || 50) + "px");
  setHdr(); addEventListener("resize", setHdr);
  paintScope();
  load(true);
  // If the agent has been working here before, keep it live in the background so
  // the editor refreshes when it finishes something.
  api(`/api/editor/${PID}`).then((st) => { if (st.thread_id) { threadId = st.thread_id; ensureChat(); } });
})();
