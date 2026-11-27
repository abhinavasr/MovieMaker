/* The assistant chat, shared by /assistant (full workspace) and the editor's Agent panel.
 *
 * AgentChat.mount({page: true})                       -- the /assistant workspace
 * AgentChat.mount({threadId, focus, onUpdate, root})  -- embedded: one fixed thread,
 *     focus() returns {shot_id, label} or null (sent with each message),
 *     onUpdate(view) is called after every poll (the editor refreshes itself).
 * Element ids are the same in both; optional ones may be missing.
 */
window.AgentChat = (() => {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const BUSY = ["queued", "planning", "executing"];
  const ICON = {image: "🖼", audio: "🔊", video: "🎬", pdf: "📄", docx: "📝", text: "📃", speech: "🔊", music: "🎵", project: "🎬", clip: "🎚"};
  const STAGE = {queued: "Waiting for the planner", planning: "Planning", executing: "Working", watching: "Watching the work", idle: "Ready", stopped: "Stopped"};
  const ago = (ts, now) => { const s = Math.max(0, (now || Date.now() / 1000) - ts);
    return s < 60 ? "just now" : s < 3600 ? Math.floor(s / 60) + " min ago" : s < 86400 ? Math.floor(s / 3600) + " h ago" : Math.floor(s / 86400) + " d ago"; };
  const dur = (s) => { s = Math.max(0, Math.floor(s)); return s < 60 ? s + "s" : Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s"; };
  const kb = (n) => n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";

  async function api(path, body) {
    const init = body === undefined ? {} : body instanceof FormData ? {method: "POST", body}
      : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
    const r = await fetch(path, init);
    if (r.status === 401) { location.href = "/login"; throw new Error("signed out"); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || data.detail || r.statusText);
    return data;
  }

  function md(src) {
    const studio = (url) => {
      const kind = /\/v\//.test(url) ? ["🎬", "Video"] : /\/i\//.test(url) ? ["🖼", "Picture"] : /\/s\//.test(url) ? ["🔊", "Speech"] : /\/m\//.test(url) ? ["🎵", "Song"] : null;
      return kind ? `<a class="ws-linkchip" href="${url}" target="_blank" rel="noopener">${kind[0]} ${kind[1]} ↗</a>` : `<a href="${url}" target="_blank" rel="noopener">${url.length > 60 ? url.slice(0, 57) + "…" : url}</a>`;
    };
    const inline = (t) => t
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<i>$2</i>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, (m, p, u) => p + studio(u));
    const out = []; let list = null;
    for (const raw of esc(src).split("\n")) {
      const line = raw.trimEnd();
      const ul = line.match(/^\s*[-*•]\s+(.*)/), ol = line.match(/^\s*\d+[.)]\s+(.*)/), h = line.match(/^#{1,4}\s+(.*)/);
      if (ul || ol) {
        const tag = ul ? "ul" : "ol";
        if (!list || list.tag !== tag) { if (list) out.push(`</${list.tag}>`); out.push(`<${tag}>`); list = {tag}; }
        out.push(`<li>${inline((ul || ol)[1])}</li>`); continue;
      }
      if (list) { out.push(`</${list.tag}>`); list = null; }
      if (h) out.push(`<h4>${inline(h[1])}</h4>`);
      else if (line) out.push(`<p>${inline(line)}</p>`);
    }
    if (list) out.push(`</${list.tag}>`);
    return out.join("");
  }

  function mediaHtml(items) {
    if (!items || !items.length) return "";
    return '<div class="ws-media">' + items.map((m) =>
      m.type === "image" ? `<img src="${esc(m.url)}" alt="generated picture" loading="lazy" data-lb="image" data-url="${esc(m.url)}">`
      : m.type === "video" ? `<video src="${esc(m.url)}" controls preload="metadata" playsinline></video>`
      : `<audio src="${esc(m.url)}" controls preload="none"></audio>`).join("") + "</div>";
  }

  function mount(opts = {}) {
    const root = opts.root || document;
    const $ = (id) => root.querySelector ? root.querySelector("#" + id) : document.getElementById(id);
    const on = (id, ev, fn) => { const el = $(id); if (el) el.addEventListener(ev, fn); };
    const page = !!opts.page;
    const ws = $("ws"), feed = $("feed"), col = $("col");
    const setHdr = () => document.documentElement.style.setProperty("--hdr", Math.round(document.querySelector("main")?.getBoundingClientRect().top + scrollY || 50) + "px");
    setHdr(); addEventListener("resize", setHdr);

    let current = opts.threadId || (page ? new URLSearchParams(location.search).get("t") : null) || null;
    let since = 0, timer = null, view = null, threads = [], pending = [], libData = null, libTab = "images";
    let lastStatus = null, unseen = false, clockSkew = 0;
    const title0 = document.title;
    const nodes = new Map(), plans = new Map();

    function toast(t) { const el = $("toast") || document.getElementById("toast"); if (!el) return; el.textContent = t; el.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(() => el.hidden = true, 2600); }

    // ---------------------------------------------------------------- feed
    const nearBottom = () => feed.scrollHeight - feed.scrollTop - feed.clientHeight < 140;
    function toBottom(force) { if (force || nearBottom()) { feed.scrollTop = feed.scrollHeight; if ($("jump")) $("jump").hidden = true; } else if ($("jump")) $("jump").hidden = false; }
    feed.addEventListener("scroll", () => { if (nearBottom() && $("jump")) $("jump").hidden = true; });
    on("jump", "click", () => toBottom(true));

    function stepHtml(m) {
      const meta = m.meta || {}, st = meta.status || "ok";
      const mark = {ok: "✓", fixed: "✓", failed: "!", running: "", fixing: ""}[st] ?? "";
      const label = m.role === "result" ? "finished" : (meta.tool || "");
      let sub = "";
      if (st === "fixing") sub = `<div class="sub">refused — the executor is repairing the call…</div>`;
      else if (st === "fixed") sub = `<div class="sub">repaired by ${esc(meta.executor)} after the studio refused the first call</div>`;
      else if (st === "failed") { let e = ""; try { e = JSON.parse(meta.result || "{}").error; } catch { e = ""; }
        sub = `<div class="sub bad">${esc(e || "failed")}</div>`; }
      const details = (meta.args ? "args " + JSON.stringify(meta.args, null, 1) + "\n\n" : "") + (meta.result ? "result " + meta.result : "");
      return `<div class="r"><span class="ic">${mark}</span><span class="why">${esc(m.content || label)}</span><span class="tool">${esc(label)}</span></div>`
        + sub + mediaHtml(meta.media) + (details ? `<pre>${esc(details)}</pre>` : "");
    }
    function planProgress(p) {
      const done = p.list.querySelectorAll(".ws-step.ok, .ws-step.fixed, .ws-step.failed").length;
      p.head.querySelector(".n").textContent = `${done} of ${p.total}`;
      p.head.querySelector(".bar i").style.width = (100 * done / Math.max(1, p.total)) + "%";
    }

    const liveEl = document.createElement("div");
    liveEl.className = "ws-live"; liveEl.hidden = true;
    liveEl.innerHTML = `<span class="ws-typing"><i></i><i></i><i></i></span><div class="b"><div><span class="stage"></span><span class="el"></span></div><div class="thought"></div></div>`;
    col.appendChild(liveEl);

    function render(m) {
      const meta = m.meta || {};
      let el = nodes.get(m.seq); const fresh = !el;
      if (m.role === "plan") {
        if (!fresh) return;
        el = document.createElement("div"); el.className = "ws-plan";
        el.innerHTML = `<div class="ws-plan-h">🧭 <b>Plan</b><span class="n"></span><span class="bar"><i></i></span></div><div class="list"></div>`;
        const p = {el, head: el.firstChild, list: el.lastChild, total: meta.steps.length, placeholders: []};
        meta.steps.forEach((s) => { const d = document.createElement("div"); d.className = "ws-step pending";
          d.innerHTML = `<div class="r"><span class="ic"></span><span class="why">${esc(s.why || s.tool)}</span><span class="tool">${esc(s.tool)}</span></div>`;
          p.list.appendChild(d); p.placeholders.push(d); });
        el.dataset.chatNode = "1"; plans.set(m.seq, p); planProgress(p);
      } else if (m.role === "step" || m.role === "result") {
        if (fresh) el = document.createElement("div");
        const open = el.classList.contains("open");
        el.className = "ws-step " + (meta.status || "ok") + (open ? " open" : "");
        el.innerHTML = stepHtml(m);
        el.querySelector(".r").onclick = () => el.classList.toggle("open");
        const p = plans.get(meta.plan);
        if (p) {
          if (fresh) { const ph = p.placeholders.shift(); if (ph) ph.replaceWith(el); else p.list.appendChild(el); nodes.set(m.seq, el); }
          planProgress(p); return;
        }
        if (fresh) { const wrap = document.createElement("div"); wrap.className = "ws-plan"; wrap.dataset.chatNode = "1"; wrap.appendChild(el); nodes.set(m.seq, el); col.insertBefore(wrap, liveEl); }
        return;
      } else if (m.role === "user") {
        if (!fresh) return;
        el = document.createElement("div"); el.style.display = "contents";
        const files = (meta.files || []).map((f) => `<span class="ws-attchip">${f.thumb ? `<img src="${esc(f.thumb)}" alt="">` : `<span class="ic">${ICON[f.kind] || "📎"}</span>`}<span>${esc(f.ref)} · ${esc(f.name)}</span></span>`).join("");
        const focus = meta.focus ? `<span class="ws-focus">🎯 ${esc(meta.focus.label || "this shot")}</span>` : "";
        el.innerHTML = `<div class="ws-msg user">${focus}${esc(m.content)}${files ? `<div class="ws-att">${files}</div>` : ""}</div>`;
      } else if (m.role === "assistant") {
        if (!fresh) return;
        el = document.createElement("div"); el.style.display = "contents";
        el.innerHTML = `<div class="ws-who">Assistant</div><div class="ws-msg bot">${md(m.content)}</div>`;
      } else {
        if (!fresh) return;
        el = document.createElement("div");
        el.className = "ws-note" + (m.role === "error" ? " err" : "") + (meta.command ? " cmd" : "");
        el.textContent = m.content;
      }
      if (fresh) { el.dataset.chatNode = "1"; nodes.set(m.seq, el); col.insertBefore(el, liveEl); }
    }

    // ---------------------------------------------------------------- status
    const elapsed = () => view && view.stage_at ? dur(Date.now() / 1000 + clockSkew - view.stage_at) : "";
    function paintStatus() {
      const t = view, st = t ? t.status : "idle";
      const busy = BUSY.includes(st), watching = st === "watching";
      const stage = STAGE[st] || st, act = t && t.activity ? t.activity : "";
      if ($("pill")) { $("pill").className = "ws-pill" + (busy ? " busy" : watching ? " watching" : ""); $("pill-dot").className = "ws-dot " + st; $("pill-t").textContent = stage.toLowerCase(); }
      if ($("st-dot")) $("st-dot").className = "ws-dot " + st;
      if ($("st-txt")) $("st-txt").innerHTML = busy || watching ? `<b>${esc(stage)}</b> · ${esc(act)} <span style="font-variant-numeric:tabular-nums">· ${elapsed()}</span>`
        : current ? "Ready — say what's next, or give feedback on anything above." : "Ready.";
      if ($("stop")) $("stop").hidden = !(busy || watching);
      if ($("status")) $("status").classList.toggle("idle", !(busy || watching));
      liveEl.hidden = !(busy || watching);
      liveEl.className = "ws-live" + (watching ? " watching" : "");
      liveEl.querySelector(".stage").textContent = busy ? (act ? act.charAt(0).toUpperCase() + act.slice(1) : stage) : "Watching running work";
      liveEl.querySelector(".el").textContent = elapsed();
      liveEl.querySelector(".thought").textContent = busy && t.thought ? "💭 " + t.thought.replace(/\s+/g, " ") : watching ? act : "";
      if (opts.onStatus) opts.onStatus(st, act);
    }
    setInterval(() => { if (view && (BUSY.includes(view.status) || view.status === "watching")) { paintStatus(); paintJobs(); } }, 1000);

    // ---------------------------------------------------------------- context panel (workspace)
    function paintJobs() {
      const box = $("jobs"); if (!box) return;
      const t = view;
      const jobs = (t?.tracked || []).filter((j) => !["succeeded", "failed", "cancelled", "done"].includes(j.status));
      // Shown only while something runs (2026-11-27: "remove running now when it's not doing anything").
      if ($("jobs-sec")) $("jobs-sec").hidden = !jobs.length;
      box.innerHTML = !jobs.length ? "" : jobs.map((j) => {
        const p = typeof j.progress === "number" && j.progress > 0 ? j.progress : null;
        return `<div class="ws-job"><div class="h"><span>${ICON[j.kind] || "⚙"} ${esc(j.label)}</span><span>${p != null ? p + "%" : esc(j.status)}</span></div>`
          + (j.message ? `<div class="m">${esc(j.message)}</div>` : "")
          + `<div class="ws-prog ${p == null ? "ind" : ""}"><i style="width:${p ?? 0}%"></i></div></div>`; }).join("");
      if ($("next-check")) $("next-check").textContent = t && t.status === "watching" && t.next_review_at ? "check-in in " + dur(t.next_review_at - Date.now() / 1000 - clockSkew) : "";
    }
    function tile({thumb, icon, tag, cap, cls = "", data = ""}) {
      return `<button type="button" class="ws-tile ${cls}" ${data}>${thumb ? `<img src="${esc(thumb)}" alt="" loading="lazy">` : `<span class="big">${icon}</span>`}`
        + (tag ? `<span class="tag">${esc(tag)}</span>` : "") + (cap ? `<span class="cap">${esc(cap)}</span>` : "") + "</button>";
    }
    function paintContext() {
      const t = view;
      const files = t?.files || [], made = t?.assets || [], pins = t?.context || [];
      if ($("files")) {
        $("files").innerHTML = files.map((f, i) => tile({thumb: f.thumb, icon: ICON[f.kind] || "📎", tag: f.ref,
          cap: f.status === "processing" ? "reading…" : f.status === "failed" ? "couldn't read" : f.name,
          cls: f.status === "processing" ? "proc" : f.status === "failed" ? "fail" : "", data: `data-file="${i}"`})).join("");
        $("files-empty").hidden = files.length > 0;
      }
      if ($("made")) {
        $("made").innerHTML = made.map((a, i) => tile({thumb: a.type === "image" ? a.url : null, icon: ICON[a.kind] || (a.type === "video" ? "🎬" : "🔊"),
          tag: a.step ? "#" + a.step : a.label, cap: a.label, data: `data-made="${i}"`})).join("");
        $("made-empty").hidden = made.length > 0;
        $("made-n").textContent = made.length || "";
      }
      if ($("pins")) {
        $("pins").innerHTML = pins.map((p, i) => `<div class="ws-li">${p.thumb ? `<img src="${esc(p.thumb)}" alt="">` : `<span class="ic">${ICON[p.kind] || "📌"}</span>`}<span title="${esc(p.title)}">${esc(p.title || p.id)}</span><button data-unpin="${i}" title="Unpin" aria-label="Unpin">✕</button></div>`).join("");
        $("pins-empty").hidden = pins.length > 0;
      }
      if ($("ctx-n")) { const n = files.length + made.length + pins.length; $("ctx-n").textContent = n ? `(${n})` : ""; }
      if ($("keep")) $("keep").textContent = t ? `This conversation is deleted on ${t.deleted_on} unless it changes. Download anything you want to keep.` : "";
      paintJobs();
    }
    on("files", "click", (e) => { const b = e.target.closest("[data-file]"); if (b) openFile(view.files[+b.dataset.file]); });
    on("made", "click", (e) => { const b = e.target.closest("[data-made]"); if (b) openMade(view.assets[+b.dataset.made]); });
    on("pins", "click", async (e) => { const b = e.target.closest("[data-unpin]"); if (!b) return;
      const p = view.context[+b.dataset.unpin]; await api(`/api/agent/threads/${current}/context`, {kind: p.kind, id: p.id, remove: true}); poll(); });

    function insertRef(text) { const ta = $("text"); ta.value += (ta.value && !/\s$/.test(ta.value) ? " " : "") + text + " "; ta.focus(); grow(); closeLb(); toast("Added to your message"); }
    const lb = () => $("lb") || document.getElementById("lb");
    function lightbox(html, actions) { const l = lb(); l.querySelector(".in").innerHTML = html + `<div class="bar">${actions}<button class="ws-btn" data-close type="button">Close</button></div>`; l.hidden = false; }
    function closeLb() { const l = lb(); if (l) { l.hidden = true; l.querySelector(".in").innerHTML = ""; } }
    if (lb()) lb().addEventListener("click", (e) => { if (e.target === lb() || e.target.closest("[data-close]")) closeLb(); const u = e.target.closest("[data-use]"); if (u) insertRef(u.dataset.use); });
    function openFile(f) {
      let body = "";
      if (f.kind === "image") body = `<img src="${esc(f.thumb || f.url)}" alt="">`;
      else if (f.kind === "video") body = `<video src="${esc(f.url)}" controls autoplay playsinline></video>`;
      else if (f.kind === "audio") body = `<audio src="${esc(f.url)}" controls autoplay></audio>`;
      if (f.excerpt) body += `<pre>${esc(f.excerpt)}${f.chars > 280 ? "\n…" : ""}</pre>`;
      const info = [f.ref, f.name, kb(f.size), f.duration_s ? f.duration_s + " s" : "", f.pages ? f.pages + " pages" : "", f.width ? f.width + "×" + f.height : "", f.status !== "ready" ? f.status : "", f.error || ""].filter(Boolean).join(" · ");
      lightbox(body + `<div class="cap">${esc(info)}</div>`, `<button class="ws-btn primary" data-use="${esc(f.ref)}" type="button">Use ${esc(f.ref)} in my message</button><a class="ws-btn" href="${esc(f.url)}" target="_blank" rel="noopener">Open original</a>`);
    }
    function openMade(a) {
      const ref = a.kind === "image" ? `picture ${a.id}` : a.kind === "video" ? `the video of project ${a.id}` : a.kind === "speech" ? `speech ${a.id}` : a.kind === "music" ? `song ${a.id}` : a.url;
      const body = a.type === "image" ? `<img src="${esc(a.url)}" alt="">` : a.type === "video" ? `<video src="${esc(a.url)}" controls autoplay playsinline></video>` : `<audio src="${esc(a.url)}" controls autoplay></audio>`;
      lightbox(body + `<div class="cap">${esc(a.label)}${a.step ? " from step #" + a.step : ""}</div>`,
        `<button class="ws-btn primary" data-use="${esc(ref)}" type="button">Refer to it in my message</button><a class="ws-btn" href="${esc(a.url)}" download target="_blank" rel="noopener">Download</a>`);
    }
    feed.addEventListener("click", (e) => { const img = e.target.closest("img[data-lb]"); if (img) lightbox(`<img src="${esc(img.dataset.url)}" alt="">`, `<a class="ws-btn" href="${esc(img.dataset.url)}" download target="_blank" rel="noopener">Download</a>`); });

    // ---------------------------------------------------------------- threads + lessons (workspace)
    async function loadThreads() {
      if (!$("threads") && !$("lessons")) return;
      const d = await api("/api/agent/threads");
      threads = d.threads; paintThreads(d.now);
      if ($("lessons")) $("lessons").innerHTML = d.lessons.length ? d.lessons.map((l) => `<div class="ws-li"><span class="ic">💡</span><span title="${esc(l.text)}">${esc(l.text)}</span><button data-forget="${esc(l.id)}" title="Forget" aria-label="Forget">✕</button></div>`).join("")
        : '<div class="ws-empty-s">Nothing yet. Tell me preferences and I keep them for every request.</div>';
    }
    function paintThreads(now) {
      if (!$("threads")) return;
      const q = ($("search")?.value || "").trim().toLowerCase();
      const list = threads.filter((t) => !q || t.title.toLowerCase().includes(q));
      $("threads").innerHTML = list.length ? list.map((t) => `<a class="ws-thr ${t.id === current ? "on" : ""}" href="?t=${esc(t.id)}" data-id="${esc(t.id)}"><span class="ws-dot ${esc(t.status)}"></span><span class="t">${esc(t.title)}</span><span class="w">${STAGE[t.status] && t.status !== "idle" ? esc(STAGE[t.status]) + " · " : ""}${ago(t.updated_at, now)}</span></a>`).join("")
        : `<div class="ws-empty-s" style="padding:6px">${q ? "No match." : "No conversations yet."}</div>`;
    }
    on("search", "input", () => paintThreads());
    on("threads", "click", (e) => { const a = e.target.closest("[data-id]"); if (!a) return; e.preventDefault(); open(a.dataset.id); ws?.classList.remove("rail-open"); });
    on("lessons", "click", async (e) => { const b = e.target.closest("[data-forget]"); if (!b) return; await api(`/api/agent/lessons/${b.dataset.forget}/delete`, {}); loadThreads(); });
    on("lesson-form", "submit", async (e) => { e.preventDefault(); const v = $("lesson-in").value.trim(); if (!v) return; await api("/api/agent/lessons", {text: v}); $("lesson-in").value = ""; loadThreads(); toast("I'll remember that"); });

    // ---------------------------------------------------------------- polling
    async function poll() {
      clearTimeout(timer);
      if (!current) { paintStatus(); return; }
      let st = view ? view.status : "idle";
      try {
        const t = await api(`/api/agent/threads/${current}?since=${since}`);
        if (t.id !== current) return;
        clockSkew = t.now - Date.now() / 1000;
        since = t.now - 1;
        const stick = nearBottom();
        if ($("empty")) $("empty").hidden = true;
        view = t;
        t.messages.forEach(render);
        if (page && $("title")) $("title").textContent = t.title;
        // The productions this chat made or worked on: one click into the editor.
        if ($("projlinks")) {
          const ps = t.projects || [];
          $("projlinks").hidden = !ps.length;
          $("projlinks").innerHTML = ps.map((p) => `<a class="ws-btn sm" href="${esc(p.url)}" title="Open “${esc(p.title)}” ${p.editor ? "in the editor" : ""}">🎬 ${esc(p.title.length > 28 ? p.title.slice(0, 26) + "…" : p.title)} ↗</a>`).join("");
        }
        paintStatus(); paintContext();
        if (t.messages.length) { if (stick) toBottom(true); else if ($("jump")) $("jump").hidden = false; }
        else if (stick && !liveEl.hidden) toBottom(true);
        if (lastStatus && lastStatus !== t.status) {
          loadThreads();
          if ((BUSY.includes(lastStatus) || lastStatus === "watching") && t.status === "idle" && document.hidden) {
            unseen = true; document.title = "● Reply ready — Story Studio";
            try { if (Notification.permission === "granted") new Notification("Story Studio", {body: "The assistant has finished: " + t.title}); } catch { /* not allowed */ }
          }
        }
        if (opts.onUpdate) opts.onUpdate(t, t.messages.length > 0 || lastStatus !== t.status);
        lastStatus = t.status; st = t.status;
      } catch (e) { console.warn(e); }
      timer = setTimeout(poll, BUSY.includes(st) ? 1000 : st === "watching" ? 4000 : 10000);
    }
    document.addEventListener("visibilitychange", () => { if (!document.hidden && unseen) { unseen = false; document.title = title0; } if (!document.hidden) poll(); });

    function open(id) {
      current = id; since = 0; view = null; lastStatus = null;
      nodes.forEach((n) => n.remove()); nodes.clear(); plans.forEach((p) => p.el.remove()); plans.clear();
      // Only what this panel drew: the page's own welcome box and prompts stay.
      col.querySelectorAll(":scope > [data-chat-node]").forEach((n) => n.remove());
      if ($("empty")) $("empty").hidden = !!id;
      if ($("del")) $("del").hidden = !id;
      if ($("projlinks")) { $("projlinks").hidden = true; $("projlinks").innerHTML = ""; }
      if (page) { if ($("title")) $("title").textContent = id ? "…" : "New conversation"; history.replaceState(null, "", id ? "?t=" + id : location.pathname); }
      paintContext(); paintStatus(); paintThreads(); poll();
      if (!id) loadThreads();
    }
    async function ensureThread() {
      if (current) return current;
      if (opts.ensureThread) { current = await opts.ensureThread(); return current; }
      const d = await api("/api/agent/threads", {});
      current = d.id; if (page) history.replaceState(null, "", "?t=" + current); if ($("del")) $("del").hidden = false;
      loadThreads(); return current;
    }

    // ---------------------------------------------------------------- files
    function paintPending() {
      if (!$("pending")) return;
      $("pending").innerHTML = pending.map((p, i) => `<span class="ws-pchip">${p.preview ? `<img src="${esc(p.preview)}" alt="">` : `<span class="ic">${ICON[p.kind] || "📎"}</span>`}<span class="n">${esc(p.name)}<span class="s">${p.state === "uploading" ? "uploading…" : p.state === "error" ? esc(p.error) : (p.ref ? p.ref + " · " : "") + kb(p.size)}</span></span><button type="button" data-rm="${i}" aria-label="Remove">✕</button></span>`).join("");
    }
    on("pending", "click", async (e) => { const b = e.target.closest("[data-rm]"); if (!b) return; const p = pending.splice(+b.dataset.rm, 1)[0]; paintPending();
      if (p?.id) { try { await api(`/api/agent/files/${p.id}/delete`, {}); poll(); } catch { /* gone */ } } });
    async function addFiles(list) {
      if (!list || !list.length) return;
      let tid;
      try { tid = await ensureThread(); } catch (e) { toast(e.message); return; }
      for (const f of list) {
        const kind = f.type.startsWith("image/") ? "image" : f.type.startsWith("audio/") ? "audio" : f.type.startsWith("video/") ? "video" : "file";
        const p = {name: f.name || "pasted file", size: f.size, kind, state: "uploading", preview: kind === "image" ? URL.createObjectURL(f) : null};
        pending.push(p); paintPending();
        const fd = new FormData(); fd.append("files", f, p.name);
        try { const d = await api(`/api/agent/threads/${tid}/files`, fd); Object.assign(p, d.files[0], {state: "ok"}); }
        catch (e) { p.state = "error"; p.error = e.message; }
        paintPending();
      }
      poll();
    }
    const pick = () => $("file")?.click();
    on("attach", "click", pick); on("attach2", "click", pick);
    on("file", "change", () => { addFiles([...$("file").files]); $("file").value = ""; });
    const dropZone = $("center") || feed.parentElement;
    let dragN = 0;
    dropZone.addEventListener("dragenter", (e) => { if ([...(e.dataTransfer?.types || [])].includes("Files")) { dragN++; dropZone.classList.add("dragging"); } });
    dropZone.addEventListener("dragleave", () => { if (--dragN <= 0) { dragN = 0; dropZone.classList.remove("dragging"); } });
    dropZone.addEventListener("dragover", (e) => e.preventDefault());
    dropZone.addEventListener("drop", (e) => { e.preventDefault(); dragN = 0; dropZone.classList.remove("dragging"); addFiles([...(e.dataTransfer?.files || [])]); });
    on("text", "paste", (e) => { const fs = [...(e.clipboardData?.files || [])]; if (fs.length) { e.preventDefault(); addFiles(fs); } });

    // ---------------------------------------------------------------- library (workspace)
    async function openLib() {
      try { await ensureThread(); } catch (e) { toast(e.message); return; }
      $("libmodal").hidden = false; $("lib-body").innerHTML = '<div class="ws-empty-s">Loading…</div>';
      libData = await api("/api/agent/library"); paintLib();
    }
    function paintLib() {
      const pinned = new Set((view?.context || []).map((p) => p.kind + ":" + p.id));
      const items = libData?.[libTab] || [];
      $("lib-tabs").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.t === libTab));
      $("lib-body").innerHTML = items.length ? `<div class="ws-grid">` + items.map((it, i) => tile({thumb: it.thumb, icon: ICON[it.kind] || "📌",
        cap: it.title, tag: it.sub || it.status || "", cls: pinned.has(it.kind + ":" + it.id) ? "pinned" : "", data: `data-pin="${i}" title="${esc(it.title)}"`})).join("") + "</div>"
        : '<div class="ws-empty-s">Nothing here yet.</div>';
    }
    on("lib-tabs", "click", (e) => { const b = e.target.closest("[data-t]"); if (b) { libTab = b.dataset.t; paintLib(); } });
    on("lib-body", "click", async (e) => {
      const b = e.target.closest("[data-pin]"); if (!b) return;
      const it = libData[libTab][+b.dataset.pin]; const isOn = b.classList.contains("pinned");
      try { const d = await api(`/api/agent/threads/${current}/context`, {kind: it.kind, id: it.id, remove: isOn});
        if (view) view.context = d.context; paintLib(); paintContext(); toast(isOn ? "Unpinned" : "Pinned — I can use it now"); }
      catch (err) { toast(err.message); }
    });
    on("lib", "click", openLib); on("lib2", "click", openLib);
    on("lib-x", "click", () => $("libmodal").hidden = true);
    on("libmodal", "click", (e) => { if (e.target === $("libmodal")) $("libmodal").hidden = true; });

    // ---------------------------------------------------------------- composer
    const COMMANDS = [["/status", "what's running, right now"], ["/review", "planner checks everything now"], ["/stop", "stop the assistant"],
      ["/remember", "keep a preference for future requests"], ["/lessons", "list what I remember"], ["/forget", "forget lesson n"], ["/help", "the commands"]];
    let slashI = 0;
    function grow() { const ta = $("text"); ta.style.height = "auto"; ta.style.height = Math.min(220, ta.scrollHeight) + "px"; }
    function paintSlash() {
      const v = $("text").value, show = /^\/\w*$/.test(v);
      const list = show ? COMMANDS.filter((c) => c[0].startsWith(v)) : [];
      $("slash").hidden = !list.length;
      slashI = Math.min(slashI, Math.max(0, list.length - 1));
      $("slash").innerHTML = list.map((c, i) => `<div data-cmd="${c[0]}" class="${i === slashI ? "on" : ""}"><b>${c[0]}</b><span>${c[1]}</span></div>`).join("");
      return list;
    }
    on("slash", "mousedown", (e) => { const d = e.target.closest("[data-cmd]"); if (!d) return; e.preventDefault(); pickCmd(d.dataset.cmd); });
    function pickCmd(c) { const arg = ["/remember", "/forget"].includes(c); $("text").value = c + (arg ? " " : ""); $("slash").hidden = true; if (!arg) send(); else $("text").focus(); }
    on("text", "input", () => { grow(); slashI = 0; paintSlash(); });
    on("text", "keydown", (e) => {
      const list = $("slash").hidden ? [] : paintSlash();
      if (list.length && (e.key === "ArrowDown" || e.key === "ArrowUp")) { e.preventDefault(); slashI = (slashI + (e.key === "ArrowDown" ? 1 : list.length - 1)) % list.length; paintSlash(); return; }
      if (list.length && (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey))) { e.preventDefault(); pickCmd(list[slashI][0]); return; }
      if (e.key === "Escape") $("slash").hidden = true;
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    });

    async function send(textOverride) {
      const text = (textOverride ?? $("text").value).trim();
      const ready = pending.filter((p) => p.state === "ok");
      if (pending.some((p) => p.state === "uploading")) { toast("Still uploading — one moment"); return; }
      if (!text && !ready.length) return;
      if ($("send")) $("send").disabled = true;
      try {
        const body = {text, files: ready.map((p) => p.id)};
        const focus = opts.focus ? opts.focus() : null;
        if (focus) body.focus = focus;
        let d;
        if (current) d = await api(`/api/agent/threads/${current}/messages`, body);
        else if (opts.ensureThread) { current = await opts.ensureThread(); d = await api(`/api/agent/threads/${current}/messages`, body); }
        else d = await api("/api/agent/threads", body);
        if (textOverride === undefined) { $("text").value = ""; grow(); }
        pending = []; paintPending(); $("slash").hidden = true;
        try { if (Notification.permission === "default") Notification.requestPermission(); } catch { /* ignore */ }
        if (d.id !== current) open(d.id); else { await poll(); toBottom(true); }
        loadThreads();
      } catch (e) { toast(e.message); }
      finally { if ($("send")) $("send").disabled = false; $("text").focus(); }
    }
    on("say", "submit", (e) => { e.preventDefault(); send(); });
    root.querySelectorAll("[data-ex]").forEach((b) => b.onclick = () => { $("text").value = b.dataset.ex; grow(); $("text").focus(); });

    // ---------------------------------------------------------------- header actions (workspace)
    on("new-chat", "click", () => { open(null); ws?.classList.remove("rail-open"); });
    on("stop", "click", async () => { if (!current) return; await api(`/api/agent/threads/${current}/stop`, {}); toast("Stopped"); poll(); });
    on("del", "click", async () => {
      if (!current || !confirm("Delete this conversation and the files you attached to it? Pictures, clips and videos it made stay in your studio.")) return;
      await api(`/api/agent/threads/${current}/delete`, {}); open(null); loadThreads();
    });
    if (page) on("title", "click", async () => {
      if (!current) return;
      const t = prompt("Rename this conversation", $("title").textContent); if (!t || !t.trim()) return;
      await api(`/api/agent/threads/${current}/rename`, {title: t.trim()}); $("title").textContent = t.trim(); loadThreads();
    });
    on("open-rail", "click", () => ws.classList.toggle("rail-open"));
    on("open-ctx", "click", () => ws.classList.toggle("ctx-open"));
    on("scrim", "click", () => ws.classList.remove("rail-open", "ctx-open"));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeLb(); if ($("libmodal")) $("libmodal").hidden = true; ws?.classList.remove("rail-open", "ctx-open"); } });

    loadThreads();
    open(current);
    return {send, poll, insert: insertRef, addFiles, get view() { return view; }, get threadId() { return current; }, setThread: (id) => open(id)};
  }
  return {mount, api, esc, md, dur};
})();
