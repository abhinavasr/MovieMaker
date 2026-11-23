// Shared by /speech and /music: submit a form, keep a history list fresh while
// anything is queued or generating, and wire the per-item actions.
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

function statusLine(it) {
  if (it.status === 'queued') {
    const pos = it.position ? `#${it.position} in the GPU queue` : 'queued';
    const why = it.message && it.message !== 'queued' ? ` — ${esc(it.message)}` : '';
    return `<span class="meta">${pos}${why}. It starts by itself when the card is free.</span>`;
  }
  if (it.status === 'generating') return '<span class="meta">Generating…</span>';
  if (it.status === 'failed') return `<span class="error meta">${esc(it.error || 'failed')}</span>`;
  return '';
}

function projectPicker(base, it, projects) {
  if (!projects.length || it.status !== 'succeeded') return '';
  return `<select data-project="${it.id}" aria-label="Add to a project">
      <option value="">Add to project…</option>
      ${projects.map(p => `<option value="${esc(p.id)}">${esc(p.title)}</option>`).join('')}
    </select>`;
}

function mediaPage({ kind, form, list, render, projects, onSubmitted }) {
  let timer = null, lastJson = '';
  const errorBox = form.querySelector('.formbar .error');
  const button = form.querySelector('button[type=submit]');

  async function refresh() {
    clearTimeout(timer);
    try {
      const r = await fetch(`/api/${kind}`);
      if (r.status === 401) { location.href = '/login'; return; }
      const { items } = await r.json();
      const json = JSON.stringify(items);
      if (json !== lastJson) {          // keep players untouched when nothing changed
        lastJson = json;
        list.innerHTML = items.length ? items.map(render).join('')
          : '<li class="empty muted">Nothing yet — your results appear here.</li>';
      }
      const busy = items.some(i => i.status === 'queued' || i.status === 'generating');
      timer = setTimeout(refresh, busy ? 3000 : 30000);
    } catch (e) { timer = setTimeout(refresh, 8000); }
  }

  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    errorBox.textContent = '';
    button.disabled = true;
    const label = button.textContent;
    button.textContent = 'Sending…';
    try {
      const r = await fetch(`/api/${kind}`, { method: 'POST', body: new FormData(form),
                                              headers: { Accept: 'application/json' } });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        // 502/503/504/52x come from Cloudflare when the link to the studio
        // blips, not from the studio itself -- say so, and check whether the
        // request landed anyway before the person resubmits.
        const edge = r.status === 502 || r.status === 503 || r.status === 504 || (r.status >= 520 && r.status <= 530);
        errorBox.textContent = body.error || (edge
          ? 'The connection to the studio dropped for a moment. Check the list below — if it isn’t there, press the button again.'
          : `Something went wrong (${r.status})`);
        if (edge) { lastJson = ''; setTimeout(refresh, 3000); }
        return;
      }
      onSubmitted && onSubmitted(body);
      lastJson = '';
      refresh();
      list.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (e) {
      errorBox.textContent = 'Could not reach the studio — check your connection.';
    } finally {
      button.disabled = false; button.textContent = label;
    }
  });

  list.addEventListener('click', async ev => {
    const del = ev.target.closest('[data-delete]');
    if (del && confirm('Delete this for good?')) {
      const r = await fetch(`/api/${kind}/${del.dataset.delete}/delete`,
                            { method: 'POST', headers: { Accept: 'application/json' } });
      if (!r.ok) alert((await r.json().catch(() => ({}))).error || 'Could not delete');
      lastJson = ''; refresh();
    }
  });
  list.addEventListener('change', async ev => {
    const sel = ev.target.closest('[data-project]');
    if (!sel || !sel.value) return;
    const fd = new FormData(); fd.append('project_id', sel.value);
    const r = await fetch(`/api/${kind}/${sel.dataset.project}/to-project`,
                          { method: 'POST', body: fd, headers: { Accept: 'application/json' } });
    const body = await r.json().catch(() => ({}));
    if (r.ok) { if (confirm('Added as audio. Open the project?')) location.href = body.project; }
    else alert(body.error || 'Could not add it');
    sel.value = '';
  });

  // Live character counters: <textarea data-count="id-of-counter" maxlength=…>
  form.querySelectorAll('[data-count]').forEach(el => {
    const out = document.getElementById(el.dataset.count);
    const max = +el.getAttribute('maxlength');
    const upd = () => { out.textContent = `${el.value.length} / ${max}`;
                        out.classList.toggle('over', el.value.length > max); };
    el.addEventListener('input', upd); upd();
  });

  refresh();
  return { refresh, projectPicker: it => projectPicker(kind, it, projects) };
}

function insertAtCursor(el, text) {
  const s = el.selectionStart ?? el.value.length, e = el.selectionEnd ?? el.value.length;
  const before = el.value.slice(0, s), after = el.value.slice(e);
  const pad = before && !before.endsWith('\n') ? '\n' : '';
  el.value = before + pad + text + after;
  el.focus();
  el.selectionStart = el.selectionEnd = (before + pad + text).length;
  el.dispatchEvent(new Event('input'));
}
