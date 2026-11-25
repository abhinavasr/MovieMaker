// Share to Batiyao: any element with data-bt-share="<kind>:<id>" opens this sheet.
// kinds: image, speech, music, project, film (editor export), shot ("<project>:<shot>").
// Post = text + the item, costs the person 1.0 BuckBuck (confirmed); Story = the item alone,
// free, pictures or videos up to 30 s, safe for everyone, 48 hours (the person's choice, 2026-11-25).
(function () {
  if (window.BatiyaoShare) return;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const css = `
  .bts-back{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;display:grid;place-items:center;padding:16px}
  .bts{width:min(520px,100%);max-height:calc(100vh - 32px);overflow:auto;background:var(--panel,#1b1824);color:var(--ink,#eee);border:1px solid var(--line,#333);border-radius:14px;padding:18px;box-shadow:0 20px 60px rgba(0,0,0,.5);font:15px/1.5 system-ui,sans-serif}
  .bts h2{margin:0 0 4px;font-size:1.15rem}.bts .m{color:var(--muted,#aaa);font-size:.85rem}
  .bts .seg{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:12px 0}
  .bts .seg button{padding:10px;border-radius:10px;border:2px solid var(--line,#444);background:transparent;color:inherit;cursor:pointer;text-align:left;font:inherit}
  .bts .seg button.on{border-color:var(--gold,var(--accent,#e0b25a))}.bts .seg button:disabled{opacity:.45;cursor:not-allowed}
  .bts .seg small{display:block;color:var(--muted,#aaa);font-size:.75rem}
  .bts textarea,.bts select{width:100%;box-sizing:border-box;background:var(--panel-2,#25212f);color:inherit;border:1px solid var(--line,#444);border-radius:8px;padding:8px;font:inherit}
  .bts label{display:block;margin:10px 0 4px;font-size:.85rem}
  .bts .row{display:flex;gap:8px;justify-content:flex-end;margin-top:14px;flex-wrap:wrap}
  .bts .btn2{padding:8px 14px;border-radius:9px;border:1px solid var(--line,#555);background:transparent;color:inherit;cursor:pointer;font:inherit}
  .bts .go{background:var(--gold,var(--accent,#e0b25a));color:var(--accent-ink,#1a1405);border-color:transparent;font-weight:600}
  .bts .go:disabled{opacity:.5}.bts .err{color:#ff8a80}.bts .ok{color:#9be59b}
  .bts .chk{display:flex;gap:8px;align-items:flex-start;margin-top:10px;font-size:.85rem}
  .bts .chk input{width:auto;flex:none;margin:3px 0 0}`;
  const style = document.createElement("style"); style.textContent = css; document.head.appendChild(style);

  async function j(url, body) {
    const r = await fetch(url, body ? {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {});
    let d = {}; try { d = await r.json(); } catch {}
    if (!r.ok) throw new Error(d.detail || d.error || `HTTP ${r.status}`);
    return d;
  }

  async function open(spec) {
    const i = spec.indexOf(":"); const kind = spec.slice(0, i), id = spec.slice(i + 1);
    const back = document.createElement("div"); back.className = "bts-back";
    back.innerHTML = `<div class="bts" role="dialog" aria-label="Share to Batiyao"><h2>Share to Batiyao</h2><div class="m">Loading…</div></div>`;
    document.body.appendChild(back);
    const close = () => back.remove();
    back.addEventListener("click", (e) => { if (e.target === back) close(); });
    const box = back.querySelector(".bts");
    let st, item;
    try { [st, item] = await Promise.all([j("/api/batiyao/status"), j(`/api/batiyao/item?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}`)]); }
    catch (e) { box.innerHTML = `<h2>Share to Batiyao</h2><p class="err">${esc(e.message)}</p><div class="row"><button class="btn2" data-x>Close</button></div>`; box.querySelector("[data-x]").onclick = close; return; }
    if (!st.enabled) { box.innerHTML = `<h2>Share to Batiyao</h2><p class="m">Batiyao isn't set up on this studio yet.</p><div class="row"><button class="btn2" data-x>Close</button></div>`; box.querySelector("[data-x]").onclick = close; return; }
    if (!st.can_share) {
      const next = location.pathname + location.search;
      box.innerHTML = `<h2>Share to Batiyao</h2>
        <p>${st.connected ? `Connected as <b>@${esc(st.username)}</b>, but Story Studio may not post for you yet.` : "Connect your Batiyao account once; after that, sharing is one click."}</p>
        <p class="m">Batiyao will ask you to allow Story Studio to upload media and publish posts as you. You can switch it off any time in Batiyao → Settings → Connected apps.</p>
        <div class="row"><button class="btn2" data-x>Cancel</button><a class="btn2 go" href="/auth/batiyao/connect?next=${encodeURIComponent(next)}">Connect Batiyao</a></div>`;
      box.querySelector("[data-x]").onclick = close; return;
    }
    let mode = "post";
    const what = item.media === "audio" ? "sound (shared as a video with a title card)" : item.media;
    box.innerHTML = `<h2>Share to Batiyao</h2>
      <div class="m">As <b>@${esc(st.username)}</b> · ${esc(item.title)} · ${esc(what)}${item.seconds ? ` · ${item.seconds} s` : ""}</div>
      <div class="seg">
        <button type="button" data-mode="post" class="on"><b>Post</b><small>With your words · costs 1.0 BuckBuck</small></button>
        <button type="button" data-mode="story" ${item.story_ok && item.media !== "audio" ? "" : "disabled"}><b>Story</b><small>${item.story_ok && item.media !== "audio" ? "Free · followers · 48 hours" : item.media === "audio" ? "Pictures and videos only" : "Videos up to 30 s only"}</small></button>
      </div>
      <div data-post>
        <label>Text (up to 500 characters; Batiyao adds the hashtags) <span class="m" data-n>0/500</span></label>
        <textarea rows="4" maxlength="500" data-text placeholder="Say something about it"></textarea>
        <label>Who is it for?</label>
        <select data-rating><option value="sfw">Everyone (safe for work)</option><option value="suggestive">Not for everyone (suggestive)</option><option value="nsfw">Adults only (explicit)</option></select>
        <div class="chk"><input type="checkbox" id="bts-spend" data-spend><label for="bts-spend" style="margin:0">Publish now and spend <b>1.0 BuckBuck</b> from my balance</label></div>
      </div>
      <div data-story hidden><p class="m">Stories have no text and reach every follower, including people under 18, so only share something suitable for everyone.</p>
        <div class="chk"><input type="checkbox" id="bts-sfw" data-sfw><label for="bts-sfw" style="margin:0">This is suitable for everyone</label></div></div>
      <p data-msg class="m"></p>
      <div class="row"><button class="btn2" data-x>Cancel</button><button class="btn2 go" data-go disabled>Publish post</button></div>`;
    const $ = (s) => box.querySelector(s);
    const ready = () => { $("[data-go]").disabled = mode === "post" ? !$("[data-spend]").checked : !$("[data-sfw]").checked; };
    box.querySelectorAll("[data-mode]").forEach((b) => b.onclick = () => {
      mode = b.dataset.mode; box.querySelectorAll("[data-mode]").forEach((x) => x.classList.toggle("on", x === b));
      $("[data-post]").hidden = mode !== "post"; $("[data-story]").hidden = mode !== "story";
      $("[data-go]").textContent = mode === "post" ? "Publish post" : "Post story"; ready();
    });
    $("[data-text]").oninput = () => { $("[data-n]").textContent = `${[...$("[data-text]").value].length}/500`; };
    $("[data-spend]").onchange = ready; $("[data-sfw]").onchange = ready;
    $("[data-x]").onclick = close;
    $("[data-go]").onclick = async () => {
      const go = $("[data-go]"); go.disabled = true; $("[data-msg]").className = "m";
      $("[data-msg]").textContent = item.media === "image" ? "Uploading…" : "Preparing and uploading the video… (a long film can take a minute)";
      try {
        const d = await j("/api/batiyao/share", {kind, id, mode, text: $("[data-text]").value, rating: mode === "story" ? "sfw" : $("[data-rating]").value, confirm_spend: mode === "post" && $("[data-spend]").checked});
        $("[data-msg]").className = "ok"; $("[data-msg]").textContent = (mode === "post" ? "Posted. " : "Story posted. ") + (d.result || "");
        go.textContent = "Done"; $("[data-x]").textContent = "Close";
      } catch (e) { $("[data-msg]").className = "err"; $("[data-msg]").textContent = e.message; go.disabled = false; }
    };
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-bt-share]"); if (!b) return;
    e.preventDefault(); e.stopPropagation(); open(b.dataset.btShare);
  }, true);
  window.BatiyaoShare = {open};
  // Back from "Connect Batiyao": say so.
  if (/[?&]batiyao=connected/.test(location.search)) {
    const n = document.createElement("div"); n.className = "bts-back"; n.style.background = "transparent"; n.style.pointerEvents = "none"; n.style.placeItems = "end center";
    n.innerHTML = `<div class="bts" style="width:auto;pointer-events:auto">Batiyao connected — press Share again to post.</div>`;
    document.body.appendChild(n); setTimeout(() => n.remove(), 5000);
    history.replaceState(null, "", location.pathname + location.search.replace(/[?&]batiyao=connected/, "").replace(/^&/, "?"));
  }
})();
