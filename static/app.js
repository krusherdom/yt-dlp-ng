/* ==========================================================================
   yt-dlp Web — frontend
   Vanilla ES2020, no build step, no dependencies.

   Design notes
   ------------
   * All application state lives in `state` (plain Maps/Sets), never in the DOM.
     The DOM is a projection of that state, patched in place.
   * Job rows are *patched*, not rebuilt: yt-dlp emits progress events many
     times per second, and rebuilding the list would destroy open log drawers,
     scroll positions and focus.
   * Every piece of server- or user-supplied text reaches the DOM through
     `textContent` (or a cloned <template>). `innerHTML` is never used with
     untrusted data.
   ========================================================================== */

window.__YTDLP_APP_LOADED = true;

(function () {
  'use strict';

  // ──────────────────────────── tiny helpers ────────────────────────────

  const $  = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  /** Clone the first element of a <template>. */
  function tpl(id) {
    const t = document.getElementById(id);
    return t.content.firstElementChild.cloneNode(true);
  }

  // ──────────────────────────────── state ───────────────────────────────

  const MAX_GLOBAL_LOG_LINES = 2000;
  const MAX_DRAWER_LINES     = 800;   // per-job drawer trim, keeps DOM small

  const state = {
    jobs:      new Map(),  // id -> job object (authoritative)
    rowEls:    new Map(),  // id -> row element (cached so drawers survive)
    expanded:  new Set(),  // job ids whose log drawer is open
    kidsOpen:  new Set(),  // parent job ids whose children list is open
    filter:    'all',
    folder:    '',         // Queue tab destination, '' == downloads root
    importFolder: '',      // Import tab destination
    candidates: [],        // [{url, extractor}]
    status:    null,
    logLines:  0
  };

  /** Cached element lookups (populated in init()). */
  const els = {};

  // ─────────────────────────────── toasts ───────────────────────────────

  function toast(message, kind) {
    const box = els.toasts;
    const el = document.createElement('div');
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.textContent = String(message);
    el.addEventListener('click', () => el.remove());
    box.appendChild(el);
    setTimeout(() => el.remove(), kind === 'error' ? 8000 : 4500);
  }

  // ──────────────────────────────── API ─────────────────────────────────

  /**
   * fetch wrapper: throws Error on non-2xx, unwrapping FastAPI's
   * `{"detail": ...}` body so the toast shows something useful.
   */
  async function api(path, opts) {
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      throw new Error('Network error: ' + (e && e.message ? e.message : e));
    }
    if (!res.ok) {
      let msg = res.status + ' ' + res.statusText;
      try {
        const body = await res.json();
        // FastAPI uses {"detail": …}; some endpoints return {"error"/"output": …}.
        const d = body && (body.detail !== undefined ? body.detail
                        : body.error !== undefined ? body.error
                        : body.output !== undefined ? body.output : undefined);
        if (d !== undefined) msg = typeof d === 'string' ? d : JSON.stringify(d);
      } catch (e) { /* body was not JSON — keep the status line */ }
      const err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    if (res.status === 204) return null;
    const ct = res.headers.get('content-type') || '';
    return ct.indexOf('application/json') !== -1 ? res.json() : res.text();
  }

  const apiJSON = (path, method, body) => api(path, {
    method: method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });

  // ───────────────────────────── formatting ─────────────────────────────

  /** speed may arrive as bytes/sec (number) or a pre-formatted string. */
  function fmtSpeed(v) {
    if (v === null || v === undefined || v === '') return '';
    if (typeof v === 'string') return v;
    if (!isFinite(v) || v <= 0) return '';
    const units = ['B/s', 'KiB/s', 'MiB/s', 'GiB/s'];
    let n = v, i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (n < 10 ? n.toFixed(1) : Math.round(n)) + ' ' + units[i];
  }

  /** eta may arrive as seconds (number) or a pre-formatted string. */
  function fmtEta(v) {
    if (v === null || v === undefined || v === '') return '';
    if (typeof v === 'string') return v;
    if (!isFinite(v) || v < 0) return '';
    const s = Math.round(v);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const pad = (x) => (x < 10 ? '0' + x : String(x));
    return h > 0 ? h + ':' + pad(m) + ':' + pad(sec) : m + ':' + pad(sec);
  }

  function pct(v) {
    const n = Number(v);
    if (!isFinite(n)) return 0;
    return Math.max(0, Math.min(100, n));
  }

  /** created_at may be an ISO string or an epoch number — sort tolerantly. */
  function sortKey(job) {
    const v = job.created_at;
    if (typeof v === 'number') return v;
    if (typeof v === 'string') {
      const t = Date.parse(v);
      if (!isNaN(t)) return t;
    }
    return 0;
  }
  const byCreated = (a, b) => (sortKey(a) - sortKey(b)) || String(a.id).localeCompare(String(b.id));

  const shortId = (id) => String(id).slice(0, 8);

  /**
   * Job-list endpoints may return a bare array or a {jobs: [...]} envelope.
   * Accept both and drop anything without an id.
   */
  function asJobList(data) {
    const arr = Array.isArray(data) ? data : (data && Array.isArray(data.jobs) ? data.jobs : []);
    return arr.filter((j) => j && j.id !== undefined);
  }

  /** How many jobs a bulk POST actually created ({jobs,count} or a bare array). */
  function queuedCount(res, fallback) {
    if (res && typeof res.count === 'number') return res.count;
    const list = asJobList(res);
    return list.length || fallback;
  }

  /** '' -> '/' for display; anything else shown verbatim. */
  const showPath = (p) => (p && p !== '/' ? p : '/');

  /** Join a folder path segment, normalising slashes. */
  function joinPath(base, name) {
    const b = String(base || '').replace(/^\/+|\/+$/g, '');
    const n = String(name || '').replace(/^\/+|\/+$/g, '');
    if (!b) return n;
    if (!n) return b;
    return b + '/' + n;
  }

  // ──────────────────────────────── tabs ────────────────────────────────

  function showTab(name) {
    $$('.tab', els.tabs).forEach((b) => {
      const on = b.dataset.tab === name;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    $$('.tabpanel').forEach((p) => { p.hidden = p.id !== 'tab-' + name; });
    if (name === 'settings') loadStatus();
  }

  // ─────────────────────────── job list render ──────────────────────────

  // Renders are coalesced into one animation frame so a burst of progress
  // events costs a single layout pass.
  let renderPending = false;
  function scheduleRender() {
    if (renderPending) return;
    renderPending = true;
    requestAnimationFrame(() => { renderPending = false; renderJobs(); });
  }

  function matchesFilter(job) {
    switch (state.filter) {
      case 'active': return job.status === 'queued' || job.status === 'running';
      case 'done':   return job.status === 'done';
      // "failed" also covers cancelled jobs — both are things you may want to retry.
      case 'failed': return job.status === 'failed' || job.status === 'cancelled';
      default:       return true;
    }
  }

  /**
   * Keyed reconcile: make `container`'s children exactly `ids`, in order,
   * moving existing nodes rather than recreating them.
   */
  function reconcile(container, ids, getEl) {
    let node = container.firstElementChild;
    for (const id of ids) {
      const el = getEl(id);
      if (node === el) { node = node.nextElementSibling; continue; }
      container.insertBefore(el, node);
    }
    while (node) {
      const next = node.nextElementSibling;
      container.removeChild(node);
      node = next;
    }
  }

  function renderJobs() {
    const jobs = state.jobs;

    // Group children under their parents; jobs whose parent is unknown are
    // treated as top-level so nothing can go missing.
    const childrenOf = new Map();
    const tops = [];
    for (const job of jobs.values()) {
      if (job.parent_id && jobs.has(job.parent_id)) {
        if (!childrenOf.has(job.parent_id)) childrenOf.set(job.parent_id, []);
        childrenOf.get(job.parent_id).push(job);
      } else {
        tops.push(job);
      }
    }
    childrenOf.forEach((arr) => arr.sort(byCreated));
    tops.sort(byCreated);

    // Drop caches for jobs the server no longer knows about.
    for (const id of Array.from(state.rowEls.keys())) {
      if (!jobs.has(id)) {
        state.rowEls.delete(id);
        state.expanded.delete(id);
        state.kidsOpen.delete(id);
      }
    }

    // A parent stays visible if it matches, or if any of its children match.
    const visible = tops.filter((j) =>
      matchesFilter(j) || (childrenOf.get(j.id) || []).some(matchesFilter));

    reconcile(els.jobs, visible.map((j) => j.id), (id) => renderRow(id, childrenOf));

    els.jobsEmpty.hidden = visible.length > 0;
    if (visible.length === 0) {
      els.jobsEmpty.textContent = jobs.size === 0
        ? 'Nothing queued yet. Paste a URL above to get started.'
        : 'No jobs match this filter.';
    }
    const n = jobs.size;
    els.jobCount.textContent = n + (n === 1 ? ' job' : ' jobs');
  }

  /** Get (or create) the row element for a job and patch it to current state. */
  function renderRow(id, childrenOf) {
    let el = state.rowEls.get(id);
    if (!el) {
      el = tpl('tpl-job');
      el.dataset.id = id;
      state.rowEls.set(id, el);
    }
    updateRow(el, state.jobs.get(id), childrenOf);
    return el;
  }

  function updateRow(el, job, childrenOf) {
    if (!job) return;
    const kids = (childrenOf && childrenOf.get(job.id)) || [];

    el.className = 'job is-' + job.status;

    // ── title / subtitle ───────────────────────────────────────────────
    const title = job.title || job.url || '(untitled)';
    const titleEl = $('.job-title', el);
    if (titleEl.textContent !== title) titleEl.textContent = title;
    titleEl.title = job.url || '';

    setText($('.job-preset', el), job.preset || '');
    setText($('.job-folder', el), showPath(job.subfolder));
    setText($('.job-file', el), job.filename || '');

    // ── status badge ───────────────────────────────────────────────────
    const badge = $('.badge', el);
    badge.className = 'badge ' + job.status;
    setText(badge, job.status || '');

    // ── progress ───────────────────────────────────────────────────────
    const done = job.status === 'done';
    const p = done ? 100 : pct(job.progress);
    $('.job-bar', el).style.transform = 'scaleX(' + (p / 100) + ')';

    // ── stats line ─────────────────────────────────────────────────────
    const bits = [];
    if (job.status === 'running' || (p > 0 && p < 100)) bits.push(p.toFixed(p % 1 ? 1 : 0) + '%');
    const sp = fmtSpeed(job.speed); if (sp && job.status === 'running') bits.push(sp);
    const eta = fmtEta(job.eta);    if (eta && job.status === 'running') bits.push('ETA ' + eta);
    if (kids.length) {   // any job with children (backend type: "playlist")
      const finished = kids.filter((k) => k.status === 'done').length;
      bits.push(finished + '/' + kids.length + ' items');
    }
    setText($('.job-stats', el), bits.join('  ·  '));

    // ── error ──────────────────────────────────────────────────────────
    const errEl = $('.job-error', el);
    if (job.error) { setText(errEl, job.error); errEl.hidden = false; }
    else { errEl.hidden = true; setText(errEl, ''); }

    // ── action buttons ─────────────────────────────────────────────────
    const act = (name) => $('[data-act="' + name + '"]', el);
    const running = job.status === 'queued' || job.status === 'running';
    act('cancel').hidden = !running;
    act('retry').hidden  = !(job.status === 'failed' || job.status === 'cancelled');
    act('delete').hidden = running;   // finish or cancel it first

    const kidsBtn = act('kids');
    if (kids.length) {
      kidsBtn.hidden = false;
      const open = state.kidsOpen.has(job.id);
      setText(kidsBtn, (open ? '▾ ' : '▸ ') + kids.length + ' item' + (kids.length === 1 ? '' : 's'));
    } else {
      kidsBtn.hidden = true;
    }

    // ── children ───────────────────────────────────────────────────────
    const kidBox = $('.job-kids', el);
    const kidsVisible = kids.length > 0 && state.kidsOpen.has(job.id);
    kidBox.hidden = !kidsVisible;
    if (kidsVisible) {
      const shown = state.filter === 'all' ? kids : kids.filter(matchesFilter);
      reconcile(kidBox, shown.map((k) => k.id), (id) => renderRow(id, childrenOf));
    }

    // ── log drawer ─────────────────────────────────────────────────────
    const drawer = $('.job-drawer', el);
    const expandBtn = $('.expand', el);
    const open = state.expanded.has(job.id);
    drawer.hidden = !open;
    setText(expandBtn, open ? '▾' : '▸');
    expandBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) ensureJobLog(job.id, el);
  }

  /** textContent write that skips no-op DOM churn. */
  function setText(node, text) {
    const s = text === null || text === undefined ? '' : String(text);
    if (node.textContent !== s) node.textContent = s;
  }

  /** Fetch the per-job log once per row element; live lines append after. */
  function ensureJobLog(id, el) {
    const pre = $('.joblog', el);
    if (pre.dataset.loaded || pre.dataset.loading) return;
    pre.dataset.loading = '1';
    pre.textContent = 'Loading log…';
    api('/api/jobs/' + encodeURIComponent(id) + '/log')
      .then((text) => {
        pre.textContent = typeof text === 'string' ? text : '';
        pre.dataset.loaded = '1';
        delete pre.dataset.loading;
        pre.scrollTop = pre.scrollHeight;
      })
      .catch((e) => {
        pre.textContent = '(no log yet: ' + e.message + ')';
        // Mark loaded anyway so live WS lines still land in the drawer.
        pre.dataset.loaded = '1';
        delete pre.dataset.loading;
      });
  }

  /** Append a live log line to an open (or cached) job drawer. */
  function appendJobLog(id, line) {
    const el = state.rowEls.get(id);
    if (!el) return;
    const pre = $('.joblog', el);
    if (!pre.dataset.loaded) return;  // the fetch-on-open will pick it up
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
    pre.appendChild(document.createTextNode(line + '\n'));
    // Trim from the front when the drawer grows unreasonably large.
    while (pre.childNodes.length > MAX_DRAWER_LINES) pre.removeChild(pre.firstChild);
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  }

  // ────────────────────────── job row interaction ───────────────────────

  function onJobsClick(ev) {
    const btn = ev.target.closest('button');
    if (!btn) return;
    const row = ev.target.closest('.job');
    if (!row) return;
    const id = row.dataset.id;

    if (btn.classList.contains('expand')) {
      if (state.expanded.has(id)) state.expanded.delete(id); else state.expanded.add(id);
      scheduleRender();
      return;
    }
    const act = btn.dataset.act;
    if (!act) return;

    if (act === 'kids') {
      if (state.kidsOpen.has(id)) state.kidsOpen.delete(id); else state.kidsOpen.add(id);
      scheduleRender();
      return;
    }
    if (act === 'cancel') return jobAction(id, 'cancel', btn);
    if (act === 'retry')  return jobAction(id, 'retry', btn);
    if (act === 'delete') return deleteJob(id, btn);
  }

  async function jobAction(id, action, btn) {
    btn.disabled = true;
    try {
      await api('/api/jobs/' + encodeURIComponent(id) + '/' + action, { method: 'POST' });
    } catch (e) {
      toast(action + ' failed: ' + e.message, 'error');
    } finally {
      btn.disabled = false;
    }
    // The authoritative update arrives over the WebSocket.
  }

  async function deleteJob(id, btn) {
    if (btn) btn.disabled = true;
    try {
      await api('/api/jobs/' + encodeURIComponent(id), { method: 'DELETE' });
      removeJobLocally(id);
    } catch (e) {
      // 404 means it is already gone (e.g. cascaded from a deleted parent).
      if (e.status === 404) removeJobLocally(id);
      else toast('Delete failed: ' + e.message, 'error');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  /** There is no "job deleted" WS event, so prune locally after a 2xx/404. */
  function removeJobLocally(id) {
    state.jobs.delete(id);
    for (const job of Array.from(state.jobs.values())) {
      if (job.parent_id === id) state.jobs.delete(job.id);
    }
    scheduleRender();
  }

  async function clearFinished() {
    const finished = Array.from(state.jobs.values())
      .filter((j) => j.status === 'done' || j.status === 'failed' || j.status === 'cancelled');
    if (!finished.length) { toast('Nothing to clear'); return; }
    els.clearFinished.disabled = true;
    let removed = 0, failed = 0;
    for (const job of finished) {
      // The job may already be gone (parent deletion cascades to children).
      if (!state.jobs.has(job.id)) continue;
      try {
        await api('/api/jobs/' + encodeURIComponent(job.id), { method: 'DELETE' });
        removeJobLocally(job.id); removed++;
      } catch (e) {
        if (e.status === 404) { removeJobLocally(job.id); removed++; }
        else failed++;
      }
    }
    els.clearFinished.disabled = false;
    toast('Cleared ' + removed + ' job' + (removed === 1 ? '' : 's') +
          (failed ? ' (' + failed + ' failed)' : ''), failed ? 'error' : 'ok');
  }

  // ─────────────────────────────── add job ──────────────────────────────

  async function addJob() {
    const url = els.url.value.trim();
    if (!url) { toast('Enter a URL first', 'error'); els.url.focus(); return; }

    // Convenience: a multi-line paste goes through the bulk endpoint.
    const urls = url.split(/\s+/).filter(Boolean);
    els.addBtn.disabled = true;
    try {
      if (urls.length > 1) {
        const res = await apiJSON('/api/jobs/bulk', 'POST', {
          urls: urls,
          preset: els.preset.value,
          subfolder: state.folder,
          extra_args: els.extraArgs.value.trim()
        });
        toast('Queued ' + queuedCount(res, urls.length) + ' jobs', 'ok');
      } else {
        await apiJSON('/api/jobs', 'POST', {
          url: urls[0],
          preset: els.preset.value,
          subfolder: state.folder,
          extra_args: els.extraArgs.value.trim()
        });
        toast('Queued', 'ok');
      }
      els.url.value = '';
    } catch (e) {
      toast('Could not queue: ' + e.message, 'error');
    } finally {
      els.addBtn.disabled = false;
    }
  }

  // ───────────────────────────── folder picker ──────────────────────────
  // One shared <dialog>, opened with a callback that receives the chosen path.

  const picker = { path: '', onPick: null };

  function openFolderPicker(startPath, onPick) {
    picker.path = startPath || '';
    picker.onPick = onPick;
    els.fdNew.value = '';
    loadFolders(picker.path);
    if (typeof els.folderDialog.showModal === 'function') els.folderDialog.showModal();
    else els.folderDialog.setAttribute('open', '');   // very old browsers
  }

  function closeFolderPicker() {
    if (typeof els.folderDialog.close === 'function') els.folderDialog.close();
    else els.folderDialog.removeAttribute('open');
  }

  async function loadFolders(path) {
    const list = els.fdList;
    list.textContent = '';
    const loading = document.createElement('p');
    loading.className = 'empty';
    loading.textContent = 'Loading…';
    list.appendChild(loading);

    try {
      const requested = String(path || '').replace(/^\/+|\/+$/g, '');
      const data = await api('/api/folders?path=' + encodeURIComponent(requested));
      // The requested path is canonical: it is always built from folder names
      // the server itself returned. The server's echoed `path` is only adopted
      // when it agrees, since some backends echo an absolute filesystem path.
      const echoed = typeof data.path === 'string' ? data.path.replace(/^\/+|\/+$/g, '') : null;
      picker.path = echoed === requested ? echoed : requested;
      renderCrumbs();
      setText(els.fdCurrent, showPath(picker.path));

      list.textContent = '';
      // Entries may be plain names or {name, path} objects — normalise both.
      const folders = (Array.isArray(data.folders) ? data.folders : [])
        .map((f) => (f && typeof f === 'object' ? String(f.name || '') : String(f || '')))
        .filter(Boolean)
        .sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
      if (picker.path) {
        const up = tpl('tpl-folder-row');
        $('.folder-name', up).textContent = '.. (up one level)';
        up.addEventListener('click', () => {
          const parts = picker.path.split('/'); parts.pop();
          loadFolders(parts.join('/'));
        });
        list.appendChild(up);
      }
      if (!folders.length && !picker.path) {
        const p = document.createElement('p');
        p.className = 'empty';
        p.textContent = 'No subfolders yet — create one below, or use the root.';
        list.appendChild(p);
      }
      for (const name of folders) {
        const row = tpl('tpl-folder-row');
        $('.folder-name', row).textContent = name;     // untrusted -> textContent
        row.addEventListener('click', () => loadFolders(joinPath(picker.path, name)));
        list.appendChild(row);
      }
    } catch (e) {
      list.textContent = '';
      const p = document.createElement('p');
      p.className = 'empty';
      p.textContent = 'Could not list folders: ' + e.message;
      list.appendChild(p);
      toast('Folder listing failed: ' + e.message, 'error');
    }
  }

  function renderCrumbs() {
    const box = els.fdCrumbs;
    box.textContent = '';
    const mk = (label, target) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = label;
      b.addEventListener('click', () => loadFolders(target));
      return b;
    };
    box.appendChild(mk('/', ''));
    if (!picker.path) return;
    const parts = picker.path.split('/').filter(Boolean);
    let acc = '';
    parts.forEach((part, i) => {
      acc = joinPath(acc, part);
      if (i > 0) {   // the root crumb is already a "/", so no separator after it
        const sep = document.createElement('span');
        sep.className = 'sep';
        sep.textContent = '/';
        box.appendChild(sep);
      }
      if (i === parts.length - 1) {
        const cur = document.createElement('span');
        cur.textContent = part;
        box.appendChild(cur);
      } else {
        box.appendChild(mk(part, acc));
      }
    });
  }

  async function createFolder() {
    const name = els.fdNew.value.trim();
    if (!name) { els.fdNew.focus(); return; }
    els.fdCreate.disabled = true;
    try {
      const target = joinPath(picker.path, name);
      await apiJSON('/api/folders', 'POST', { path: target });
      els.fdNew.value = '';
      toast('Folder created', 'ok');
      await loadFolders(target);   // drill into the folder that was just created
    } catch (e) {
      toast('Create failed: ' + e.message, 'error');
    } finally {
      els.fdCreate.disabled = false;
    }
  }

  function setQueueFolder(path) {
    state.folder = path || '';
    setText(els.folderLabel, showPath(state.folder));
  }
  function setImportFolder(path) {
    state.importFolder = path || '';
    setText(els.importFolderLabel, showPath(state.importFolder));
  }

  // ──────────────────────────────── import ──────────────────────────────

  async function scanImport() {
    const file = els.importFile.files && els.importFile.files[0];
    const text = els.importText.value.trim();
    if (!file && !text) { toast('Paste some links or pick an HTML file', 'error'); return; }

    // multipart/form-data — do NOT set Content-Type, the browser adds the boundary.
    const fd = new FormData();
    if (file) fd.append('file', file, file.name);
    else fd.append('text', text);

    els.scanBtn.disabled = true;
    try {
      const data = await api('/api/import', { method: 'POST', body: fd });
      state.candidates = Array.isArray(data.candidates) ? data.candidates : [];
      const rejected = Array.isArray(data.rejected) ? data.rejected.length : 0;
      renderCandidates(rejected);
      els.importResults.hidden = false;
      if (!state.candidates.length) toast('No supported URLs found', 'error');
    } catch (e) {
      toast('Scan failed: ' + e.message, 'error');
    } finally {
      els.scanBtn.disabled = false;
    }
  }

  function renderCandidates(rejectedCount) {
    const box = els.candidates;
    box.textContent = '';
    for (const c of state.candidates) {
      const row = tpl('tpl-candidate');
      $('.cand-url', row).textContent = c.url || '';            // untrusted
      $('.cand-extractor', row).textContent = c.extractor || '?';
      row.querySelector('input').dataset.url = c.url || '';
      box.appendChild(row);
    }
    setText(els.importSummary,
      state.candidates.length + ' supported link' + (state.candidates.length === 1 ? '' : 's') +
      (rejectedCount ? '  ·  ' + rejectedCount + ' rejected' : ''));
    updateSelectedCount();
  }

  function selectedUrls() {
    return $$('input[type="checkbox"]', els.candidates)
      .filter((cb) => cb.checked)
      .map((cb) => cb.dataset.url)
      .filter(Boolean);
  }

  function updateSelectedCount() {
    const n = selectedUrls().length;
    setText(els.importAddBtn, 'Add ' + n + ' selected');
    els.importAddBtn.disabled = n === 0;
  }

  async function addSelected() {
    const urls = selectedUrls();
    if (!urls.length) return;
    els.importAddBtn.disabled = true;
    try {
      const res = await apiJSON('/api/jobs/bulk', 'POST', {
        urls: urls,
        preset: els.importPreset.value,
        subfolder: state.importFolder
      });
      toast('Queued ' + queuedCount(res, urls.length) + ' jobs', 'ok');
      resetImport();
      showTab('queue');
    } catch (e) {
      toast('Bulk add failed: ' + e.message, 'error');
    } finally {
      updateSelectedCount();
    }
  }

  function resetImport() {
    state.candidates = [];
    els.candidates.textContent = '';
    els.importResults.hidden = true;
    els.importText.value = '';
    els.importFile.value = '';
  }

  // ───────────────────────────── global log ─────────────────────────────

  function appendGlobalLog(jobId, line) {
    const box = els.globalLog;
    const div = document.createElement('div');
    const idSpan = document.createElement('span');
    idSpan.className = 'jid';
    idSpan.textContent = shortId(jobId) + ' ';
    div.appendChild(idSpan);
    div.appendChild(document.createTextNode(String(line)));   // untrusted -> text node
    box.appendChild(div);
    state.logLines++;

    while (box.childElementCount > MAX_GLOBAL_LOG_LINES) {
      box.removeChild(box.firstElementChild);
      state.logLines--;
    }
    setText(els.logCount, state.logLines + ' line' + (state.logLines === 1 ? '' : 's'));

    if (els.autoscroll.checked) box.scrollTop = box.scrollHeight;
  }

  /**
   * Seed the Logs tab from the backend's in-memory ring buffer so a page
   * reload does not start from an empty view. Lines are stored as
   * "<job_id> | <text>" ('-' when the line has no job).
   */
  function seedLogLines(lines) {
    for (const raw of lines) {
      const s = String(raw);
      const i = s.indexOf(' | ');
      if (i > 0 && s.slice(0, i).indexOf(' ') === -1) appendGlobalLog(s.slice(0, i), s.slice(i + 3));
      else appendGlobalLog('-', s);
    }
  }

  async function seedGlobalLog() {
    try {
      const data = await api('/api/logs');
      if (state.logLines === 0 && data && Array.isArray(data.lines)) seedLogLines(data.lines);
    } catch (e) { /* optional endpoint — the live stream still works */ }
  }

  /** Keep the preset dropdowns in sync with what the backend actually offers. */
  async function syncPresets() {
    let data;
    try { data = await api('/api/presets'); } catch (e) { return; }   // keep the static options
    const names = data && Array.isArray(data.presets) ? data.presets : null;
    if (!names || !names.length) return;
    for (const sel of [els.preset, els.importPreset]) {
      const want = names.indexOf(sel.value) !== -1 ? sel.value : (data.default || names[0]);
      sel.textContent = '';
      for (const name of names) {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;          // untrusted -> textContent
        sel.appendChild(opt);
      }
      sel.value = want;
    }
  }

  function clearGlobalLog() {
    els.globalLog.textContent = '';
    state.logLines = 0;
    setText(els.logCount, '0 lines');
  }

  // ────────────────────────────── settings ──────────────────────────────

  /** Paint the header pills and the Settings tab from a status payload. */
  function applyStatus(s) {
    if (!s) return;
    state.status = s;
    setText(els.stVersion, s.ytdlp_version || 'unknown');
    setText(els.stCookies, s.cookies_detected ? 'detected' : 'not found');
    setText(els.stConcurrent, s.max_concurrent !== undefined ? String(s.max_concurrent) : '—');
    setText(els.stRoot, s.downloads_root || '—');

    setText(els.versionPill, 'yt-dlp ' + (s.ytdlp_version || '—'));
    els.cookiesPill.className = 'pill ' + (s.cookies_detected ? 'ok' : 'off');
    setText(els.cookiesPill, s.cookies_detected ? 'cookies ✓' : 'no cookies');
  }

  async function loadStatus() {
    try {
      applyStatus(await api('/api/status'));
    } catch (e) {
      toast('Could not load status: ' + e.message, 'error');
    }
  }

  async function updateYtdlp() {
    els.updateBtn.disabled = true;
    els.updateOutput.hidden = false;
    els.updateOutput.textContent = 'Updating yt-dlp…';
    try {
      const r = await apiJSON('/api/update-ytdlp', 'POST', {});
      // `restart_required` is the documented field; the backend also reports
      // `changed` + a `note`, so accept either.
      const changed = r.restart_required !== undefined ? r.restart_required
                    : (r.changed !== undefined ? r.changed : r.old_version !== r.new_version);
      const lines = [
        'old version: ' + (r.old_version || '?'),
        'new version: ' + (r.new_version || '?'),
        'restart required: ' + (changed ? 'yes — restart the container to load it' : 'no')
      ];
      if (r.note) lines.push(String(r.note));
      lines.push('', String(r.output || ''));
      els.updateOutput.textContent = lines.join('\n');
      toast(changed ? 'Updated to ' + r.new_version : 'Already up to date', 'ok');
      loadStatus();
    } catch (e) {
      els.updateOutput.textContent = 'Update failed: ' + e.message;
      toast('Update failed: ' + e.message, 'error');
    } finally {
      els.updateBtn.disabled = false;
    }
  }

  // ───────────────────────────── WebSocket ──────────────────────────────

  let ws = null;
  let wsDelay = 1000;          // backoff, doubles to 30s
  let wsTimer = null;
  let wsPing = null;

  function setConn(kind, text) {
    els.connDot.className = 'dot ' + kind;
    setText(els.connText, text);
  }

  function connectWS() {
    clearTimeout(wsTimer);
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = proto + '//' + location.host + '/ws';
    setConn('wait', 'connecting');

    try {
      ws = new WebSocket(url);
    } catch (e) {
      scheduleReconnect();
      return;
    }

    ws.onopen = () => {
      wsDelay = 1000;
      setConn('on', 'live');
      // The server only reads to detect disconnects; a periodic ping keeps
      // idle-timeout proxies from dropping the socket.
      clearInterval(wsPing);
      wsPing = setInterval(() => {
        if (ws && ws.readyState === 1) {
          try { ws.send('ping'); } catch (e) { /* closing */ }
        }
      }, 30000);
    };

    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      handleWSMessage(msg);
    };

    ws.onclose = () => {
      clearInterval(wsPing);
      setConn('off', 'offline');
      scheduleReconnect();
    };

    ws.onerror = () => { /* onclose always follows; handled there */ };
  }

  function scheduleReconnect() {
    clearTimeout(wsTimer);
    const delay = wsDelay;
    setConn('off', 'retrying in ' + Math.round(delay / 1000) + 's');
    wsTimer = setTimeout(connectWS, delay);
    wsDelay = Math.min(wsDelay * 2, 30000);
  }

  function handleWSMessage(msg) {
    if (!msg || !msg.type) return;

    if (msg.type === 'snapshot') {
      // A snapshot is authoritative: it replaces local job state entirely.
      state.jobs.clear();
      for (const job of asJobList(msg.jobs)) state.jobs.set(job.id, job);
      scheduleRender();
      // The snapshot also carries status and a log tail; use them so a fresh
      // page has everything after a single round trip.
      if (msg.status) applyStatus(msg.status);
      if (Array.isArray(msg.logs) && state.logLines === 0) seedLogLines(msg.logs);
      return;
    }

    if (msg.type === 'job' && msg.job && msg.job.id !== undefined) {
      state.jobs.set(msg.job.id, msg.job);
      scheduleRender();
      return;
    }

    // The backend emits {"type":"deleted", job_id, child_ids} when a job (and
    // any children it cascaded to) is removed.
    if (msg.type === 'deleted') {
      const ids = [msg.job_id].concat(Array.isArray(msg.child_ids) ? msg.child_ids : []);
      let changed = false;
      for (const id of ids) if (id !== undefined && state.jobs.delete(id)) changed = true;
      if (changed) scheduleRender();
      return;
    }

    if (msg.type === 'log' && msg.job_id !== undefined) {
      const line = msg.line === undefined || msg.line === null ? '' : String(msg.line);
      appendGlobalLog(msg.job_id, line);
      appendJobLog(msg.job_id, line);
    }
  }

  // ─────────────────────────────── bootstrap ────────────────────────────

  function cacheEls() {
    els.tabs           = $('#tabs');
    els.toasts         = $('#toasts');

    els.versionPill    = $('#version-pill');
    els.cookiesPill    = $('#cookies-pill');
    els.connDot        = $('#conn-dot');
    els.connText       = $('#conn-text');

    els.url            = $('#url');
    els.preset         = $('#preset');
    els.extraArgs      = $('#extra-args');
    els.extraWrap      = $('#extra-wrap');
    els.extraToggle    = $('#extra-toggle');
    els.addBtn         = $('#add-btn');
    els.folderBtn      = $('#folder-btn');
    els.folderLabel    = $('#folder-label');

    els.filters        = $('#filters');
    els.jobs           = $('#jobs');
    els.jobsEmpty      = $('#jobs-empty');
    els.jobCount       = $('#job-count');
    els.clearFinished  = $('#clear-finished');

    els.importText     = $('#import-text');
    els.importFile     = $('#import-file');
    els.scanBtn        = $('#scan-btn');
    els.importReset    = $('#import-reset');
    els.importResults  = $('#import-results');
    els.importSummary  = $('#import-summary');
    els.candidates     = $('#candidates');
    els.importPreset   = $('#import-preset');
    els.importFolderBtn   = $('#import-folder-btn');
    els.importFolderLabel = $('#import-folder-label');
    els.importAddBtn   = $('#import-add-btn');
    els.selAll         = $('#sel-all');
    els.selNone        = $('#sel-none');

    els.globalLog      = $('#global-log');
    els.autoscroll     = $('#autoscroll');
    els.clearLog       = $('#clear-log');
    els.logCount       = $('#log-count');

    els.stVersion      = $('#st-version');
    els.stCookies      = $('#st-cookies');
    els.stConcurrent   = $('#st-concurrent');
    els.stRoot         = $('#st-root');
    els.updateBtn      = $('#update-btn');
    els.updateOutput   = $('#update-output');

    els.folderDialog   = $('#folder-dialog');
    els.fdCrumbs       = $('#fd-crumbs');
    els.fdList         = $('#fd-list');
    els.fdNew          = $('#fd-new');
    els.fdCreate       = $('#fd-create');
    els.fdUse          = $('#fd-use');
    els.fdClose        = $('#fd-close');
    els.fdCurrent      = $('#fd-current');
  }

  function wire() {
    // tabs
    els.tabs.addEventListener('click', (e) => {
      const b = e.target.closest('.tab');
      if (b) showTab(b.dataset.tab);
    });

    // queue: add bar
    els.addBtn.addEventListener('click', addJob);
    els.url.addEventListener('keydown', (e) => { if (e.key === 'Enter') addJob(); });
    els.extraToggle.addEventListener('click', () => {
      const open = els.extraWrap.hidden;
      els.extraWrap.hidden = !open;
      els.extraToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      els.extraToggle.classList.toggle('btn-primary', open);
      if (open) els.extraArgs.focus();
    });
    els.folderBtn.addEventListener('click', () =>
      openFolderPicker(state.folder, setQueueFolder));

    // queue: filters + list
    els.filters.addEventListener('click', (e) => {
      const b = e.target.closest('.chip');
      if (!b) return;
      state.filter = b.dataset.filter;
      $$('.chip', els.filters).forEach((c) => c.classList.toggle('active', c === b));
      scheduleRender();
    });
    els.jobs.addEventListener('click', onJobsClick);
    els.clearFinished.addEventListener('click', clearFinished);

    // import
    els.scanBtn.addEventListener('click', scanImport);
    els.importReset.addEventListener('click', resetImport);
    els.candidates.addEventListener('change', updateSelectedCount);
    els.selAll.addEventListener('click', () => {
      $$('input[type="checkbox"]', els.candidates).forEach((cb) => { cb.checked = true; });
      updateSelectedCount();
    });
    els.selNone.addEventListener('click', () => {
      $$('input[type="checkbox"]', els.candidates).forEach((cb) => { cb.checked = false; });
      updateSelectedCount();
    });
    els.importAddBtn.addEventListener('click', addSelected);
    els.importFolderBtn.addEventListener('click', () =>
      openFolderPicker(state.importFolder, setImportFolder));

    // logs
    els.clearLog.addEventListener('click', clearGlobalLog);

    // settings
    els.updateBtn.addEventListener('click', updateYtdlp);

    // folder dialog
    els.fdClose.addEventListener('click', closeFolderPicker);
    els.fdCreate.addEventListener('click', createFolder);
    els.fdNew.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); createFolder(); } });
    els.fdUse.addEventListener('click', () => {
      if (picker.onPick) picker.onPick(picker.path);
      closeFolderPicker();
    });
    // Clicking the backdrop closes the dialog. A click on the dialog's own
    // padding also targets the dialog element, so compare against its box.
    els.folderDialog.addEventListener('click', (e) => {
      if (e.target !== els.folderDialog) return;
      const r = els.folderDialog.getBoundingClientRect();
      const inside = e.clientX >= r.left && e.clientX <= r.right &&
                     e.clientY >= r.top  && e.clientY <= r.bottom;
      if (!inside) closeFolderPicker();
    });
  }

  function init() {
    cacheEls();
    wire();
    setQueueFolder('');
    setImportFolder('');
    updateSelectedCount();

    loadStatus();
    syncPresets();
    seedGlobalLog();

    // Seed the queue over REST so the list is populated even before the
    // WebSocket snapshot arrives (the snapshot then replaces it).
    api('/api/jobs')
      .then((data) => {
        for (const job of asJobList(data)) state.jobs.set(job.id, job);
        scheduleRender();
      })
      .catch((e) => toast('Could not load jobs: ' + e.message, 'error'));

    connectWS();
    renderJobs();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
