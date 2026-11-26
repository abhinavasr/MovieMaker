// A podcast run, live: the tiers as they finish, the sources and fact-checks, the debate
// points (editable at the review gate), the script, and the way into the editor.
(() => {
  const root = document.getElementById("pc-run");
  let run = window.PODCAST, timer = null, editing = false;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const ACTIVE = ["queued", "researching", "debating", "writing", "building"];
  const host = (side) => run.hosts[side === "a" ? 0 : 1];
  const VERDICT = {"Verified": "ok", "Partially Verified": "part", "Needs Context": "ctx", "Disputed": "bad"};

  async function call(path, body) {
    const r = await fetch(`/api/podcasts/${run.podcast_id}${path}`, body === undefined ? {} :
      {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || d.error || `HTTP ${r.status}`);
    return d;
  }

  function sources(side) {
    const list = (run.sources || []).filter((s) => s.side === side);
    if (!list.length) return "";
    return `<ul class="pc-src">${list.map((s) => `<li><span class="pc-sid">${esc(s.id)}</span>
      <a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a> <span class="muted">${esc(s.type)}</span>
      <div class="muted">${esc(s.summary)}</div>
      ${s.quote ? `<blockquote class="${s.quote_ok ? "" : "warn"}">“${esc(s.quote)}” <span title="${s.quote_ok ? "Found word for word on the page" : "Not found word for word on the page"}">${s.quote_ok ? "✓ on the page" : "⚠ not on the page"}</span></blockquote>` : ""}</li>`).join("")}</ul>`;
  }

  function facts(side) {
    const f = run[`${side}_factcheck`];
    if (!f) return "";
    return `<ul class="pc-facts">${f.verdicts.map((v) => `<li><span class="pc-v ${VERDICT[v.status] || ""}">${esc(v.status)}</span> ${esc(v.statement)}
      <div class="muted">${esc(v.note)}</div>
      ${v.evidence.length ? `<div class="pc-ev">${v.evidence.map((e) => `<a href="${esc(e.url)}" target="_blank" rel="noopener" title="${esc(e.quote)}">${esc(e.title).slice(0, 60)}</a>${e.quote_ok ? " ✓" : " ⚠"}`).join(" · ")}</div>` : ""}</li>`).join("")}</ul>
      ${(f.withdrawn || []).length ? `<div class="pc-guard"><b>Withdrawn by the researcher after the fact-check</b><ul>${f.withdrawn.map((w) => `<li><s>${esc(w.statement)}</s> <span class="muted">${esc(w.status)}${w.why ? ` — ${esc(w.why)}` : ""}</span></li>`).join("")}</ul></div>` : ""}
      ${f.revised && (f.first_round || []).length ? `<details class="pc-guard"><summary>First round, before the researcher revised (${esc(f.first_round.length)})</summary><ul class="pc-facts">${f.first_round.map((v) => `<li><span class="pc-v ${VERDICT[v.status] || ""}">${esc(v.status)}</span> ${esc(v.statement)}<div class="muted">${esc(v.note)}</div></li>`).join("")}</ul></details>` : ""}
      ${f.guardrails.length ? `<div class="pc-guard"><b>Guardrails</b><ul>${f.guardrails.map((g) => `<li>${esc(g)}</li>`).join("")}</ul></div>` : ""}`;
  }

  function persona(side) {
    const p = run[`${side}_persona`];
    if (!p) return "";
    return `<p><b>Stance:</b> ${esc(p.coreStance)}</p><ol class="pc-fp">${p.frictionPoints.map((fp) => `<li><b>${esc(fp.title)}</b> <span class="muted">intensity ${esc(fp.intensity)}</span>
      <div class="muted">${esc(fp.philosophicalRoot)} — ${esc(fp.critiqueOfOpponent)}</div></li>`).join("")}</ol>`;
  }

  function sideCard(side) {
    const h = host(side), syn = run[`${side}_synthesis`];
    if (!syn && !(run.sources || []).some((s) => s.side === side)) return "";
    return `<section class="card pc-side"><h2>${esc(h.name)} <span class="muted">${esc(h.lens)}</span></h2>
      ${syn ? `<p><b>Thesis:</b> ${esc(syn.thesis)}</p>` : ""}
      <details ${run.status === "review_research" ? "open" : ""}><summary>Sources (${(run.sources || []).filter((s) => s.side === side).length})</summary>${sources(side)}</details>
      ${run[`${side}_factcheck`] ? `<details open><summary>Fact-checks${run[`${side}_factcheck`].revised ? " (after the researcher's revision and a re-check)" : ""}</summary>${facts(side)}</details>` : ""}
      ${run[`${side}_persona`] ? `<details ${run.status === "review_research" ? "open" : ""}><summary>Friction points</summary>${persona(side)}</details>` : ""}
    </section>`;
  }

  const FIELDS = [["title", "Point"], ["debateQuestion", "The question"], ["aPosition", () => `${run.hosts[0].name}'s position`],
                  ["bPosition", () => `${run.hosts[1].name}'s position`], ["factualGround", "Shared facts"], ["philosophicalVulnerability", "The tension"]];

  function pointsView(o) {
    const editable = run.status === "review_points";
    const pts = o.points.map((p, i) => editable ? `<div class="pc-point" data-i="${i}">
        ${FIELDS.map(([k, l]) => `<label>${esc(typeof l === "function" ? l() : l)}<textarea rows="${k === "title" ? 1 : 2}" data-k="${k}">${esc(p[k])}</textarea></label>`).join("")}
        <button class="btn tiny danger" type="button" data-drop="${i}">Remove this point</button></div>` :
      `<li><b>${esc(p.title)}</b><div>${esc(p.debateQuestion)}</div>
        <div class="muted"><b>${esc(run.hosts[0].name)}:</b> ${esc(p.aPosition)}<br><b>${esc(run.hosts[1].name)}:</b> ${esc(p.bPosition)}<br><b>Shared facts:</b> ${esc(p.factualGround)}</div></li>`);
    return `<section class="card"><h2>Where they collide <span class="muted">Tier 2 · orchestrator</span></h2>
      <p>${esc(o.summary)}</p>
      ${editable ? `<div class="pc-points">${pts.join("")}</div><div class="formbar"><button class="btn" type="button" data-add>＋ Add a point</button></div>` : `<ol class="pc-fp">${pts.join("")}</ol>`}
      ${o.discarded.length ? `<details><summary>Left out (${o.discarded.length})</summary><ul>${o.discarded.map((d) => `<li>${esc(d.pointTitle)} <span class="muted">(${d.origin === "A" ? esc(run.hosts[0].name) : esc(run.hosts[1].name)}) — ${esc(d.reason)}</span></li>`).join("")}</ul></details>` : ""}
    </section>`;
  }

  function scriptView() {
    if (!run.script) return "";
    const names = {A: run.hosts[0].name, B: run.hosts[1].name};
    const n = run.script.reduce((a, s) => a + s.lines.length, 0);
    const words = run.script.reduce((a, s) => a + s.lines.reduce((b, l) => b + l.text.split(/\s+/).length, 0), 0);
    return `<section class="card"><h2>Script <span class="muted">${n} lines · ~${(words / 150).toFixed(1)} min</span></h2>
      ${run.script.map((s) => `<h3>${esc(s.title)}</h3>${s.lines.map((l) => `<p class="pc-line ${l.speaker}"><b>${esc(names[l.speaker])}</b>
        ${l.delivery ? `<i class="muted">(${esc(l.delivery)})</i>` : ""} ${esc(l.text)} ${l.cites.length ? `<span class="pc-cite">${l.cites.map(esc).join(" ")}</span>` : ""}</p>`).join("")}`).join("")}
    </section>`;
  }

  function header() {
    const st = run.status, active = ACTIVE.includes(st);
    const pill = st === "done" ? "succeeded" : ["failed", "cancelled"].includes(st) ? "failed" : "running";
    const done = (run.steps || []).filter((s) => s.done).length;
    return `<h1>${esc(run.title || run.topic.slice(0, 90))}</h1>
      <p class="muted">${esc(run.topic)}</p>
      <p class="muted">${esc(run.hosts[0].name)} (${esc(run.hosts[0].lens)}) vs ${esc(run.hosts[1].name)} (${esc(run.hosts[1].lens)}) · about ${run.minutes} min · ${run.pages_read || 0} web pages read</p>
      <div class="banner pc-status ${st}">
        <div class="pc-row"><span class="pill ${pill}">${esc(st.replace("_", " "))}</span><span>${esc(run.message)}</span></div>
        <div class="bar"><div style="width:${run.progress}%"></div>${active ? '<i class="pc-live"></i>' : ""}</div>
        ${run.error ? `<div class="error">${esc(run.error)}</div>` : ""}
        <div class="formbar">
          ${st === "done" && run.editor_path ? `<a class="btn primary" href="${esc(run.editor_path)}">Open in the editor →</a>
            <span class="muted">Next: choose the hosts' looks there; their voices are designed and every line voiced, then draw the shots and render.</span>` : ""}
          ${st === "review_research" ? `<button class="btn primary" data-go>Looks good — find where they collide →</button>` : ""}
          ${st === "review_points" ? `<button class="btn primary" data-go>Write the script with these points →</button>` : ""}
          ${active || st.startsWith("review") ? `<button class="btn danger" data-cancel>Cancel</button>` : ""}
          ${["failed", "cancelled"].includes(st) ? `<button class="btn primary" data-retry>Try again from where it stopped</button>` : ""}
          ${!active ? `<button class="btn tiny danger" data-delete>Delete</button>` : ""}
          <span class="muted" data-msg></span>
        </div>
      </div>
      <details class="card pc-steps" ${active ? "open" : ""}><summary>The agents (${done}/${(run.steps || []).length})</summary>
        <ol>${(run.steps || []).map((s) => `<li class="${s.done ? "done" : ""}">${s.done ? "✓" : "○"} ${esc(s.label)}</li>`).join("")}</ol></details>`;
  }

  function paint() {
    if (editing && run.status === "review_points") return;   // never overwrite the person's edits
    root.innerHTML = header() + sideCard("a") + sideCard("b") + (run.orchestrator ? pointsView(run.orchestrator) : "") + scriptView() +
      `<details class="card pc-log"><summary>Log</summary><ol>${(run.log || []).slice().reverse().map((l) => `<li><span class="muted">${new Date(l.t * 1000).toLocaleTimeString()}</span> ${esc(l.text)}</li>`).join("")}</ol></details>`;
    wire();
  }

  function readPoints() {
    return [...root.querySelectorAll(".pc-point")].map((el) => Object.fromEntries(
      [...el.querySelectorAll("[data-k]")].map((t) => [t.dataset.k, t.value.trim()])));
  }

  function wire() {
    const msg = (t) => { const m = root.querySelector("[data-msg]"); if (m) m.textContent = t; };
    root.querySelectorAll(".pc-point textarea").forEach((t) => t.addEventListener("input", () => { editing = true; }));
    root.querySelector("[data-add]")?.addEventListener("click", () => {
      run.orchestrator.points = readPoints().concat([{title: "", debateQuestion: "", aPosition: "", bPosition: "", factualGround: "", philosophicalVulnerability: ""}]);
      editing = false; paint(); editing = true;
    });
    root.querySelectorAll("[data-drop]").forEach((b) => b.onclick = () => {
      const pts = readPoints(); pts.splice(Number(b.dataset.drop), 1); run.orchestrator.points = pts;
      editing = false; paint(); editing = true;
    });
    root.querySelector("[data-go]")?.addEventListener("click", async (e) => {
      e.target.disabled = true; msg("Continuing…");
      try { run = await call("/continue", run.status === "review_points" ? {points: readPoints()} : {}); editing = false; paint(); poll(); }
      catch (err) { msg(err.message); e.target.disabled = false; }
    });
    root.querySelector("[data-cancel]")?.addEventListener("click", async () => {
      if (!confirm("Cancel this podcast? What is done so far is kept; you can try again later.")) return;
      try { run = await call("/cancel", {}); paint(); } catch (err) { msg(err.message); }
    });
    root.querySelector("[data-retry]")?.addEventListener("click", async () => {
      try { run = await call("/retry", {}); paint(); poll(); } catch (err) { msg(err.message); }
    });
    root.querySelector("[data-delete]")?.addEventListener("click", async () => {
      if (!confirm("Delete this podcast's research and script? (A production it made stays in the editor.)")) return;
      try { await call("/delete", {}); location.href = "/podcast"; } catch (err) { msg(err.message); }
    });
  }

  async function poll() {
    clearTimeout(timer);
    try { run = await call(""); paint(); } catch { /* try again */ }
    if (ACTIVE.includes(run.status)) timer = setTimeout(poll, 3000);
  }

  paint();
  if (ACTIVE.includes(run.status)) timer = setTimeout(poll, 3000);
})();
