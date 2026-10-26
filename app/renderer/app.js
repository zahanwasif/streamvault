// StreamVault renderer: plain DOM, talks to the main process only through window.sv.
const sv = window.api; // `api` itself is taken: contextBridge defines it as a global
const $ = sel => document.querySelector(sel);
const PRIORITIES = ['high', 'normal', 'low'];
const PRIO_LABEL = { high: 'High', normal: 'Normal', low: 'Low' };
const STAGES = ['download', 'transcribe', 'pack', 'signals', 'clip', 'export'];
const STAGE_LABEL = {
  download: 'Download', transcribe: 'Transcribe', pack: 'Pack', signals: 'Signals', clip: 'Find & render', export: 'To OneDrive',
};
const STATUS_LABEL = {
  queued: 'Queued', running: 'Working', retry: 'Will retry', done: 'Done', failed: 'Failed', cancelled: 'Cancelled',
};

const jobs = new Map();
let pending = [];             // adds the engine hasn't picked up yet
let engineState = { state: 'starting', message: '' };
const logs = new Map();       // job id -> recent live log lines
const openLogs = new Set();   // job ids whose log panel is open
let shorts = [];
let filter = 'waiting';
let settings = {};

// ------------------------------------------------------------------ helpers

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (k === 'value') node.value = v;
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) node.append(c);
  return node;
}

let toastTimer;
function toast(msg, kind = '') {
  const t = $('#toast');
  t.textContent = msg;
  t.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = 'toast'; }, 3200);
}

// A High / Normal / Low toggle; the chosen value is in container.dataset.value.
function seg(container, value) {
  container.dataset.value = value;
  container.replaceChildren(...PRIORITIES.map(p => el('button', {
    class: `p-${p}${p === value ? ' on' : ''}`, type: 'button',
    onclick: e => {
      [...container.children].forEach(x => x.classList.remove('on'));
      e.currentTarget.classList.add('on');
      container.dataset.value = p;
    },
  }, PRIO_LABEL[p])));
}

function ago(iso) {
  if (!iso) return '';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function basename(p) {
  return String(p).split(/[\\/]/).pop();
}

// ------------------------------------------------------------------ tabs

function showTab(name) {
  document.querySelectorAll('.nav').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.id === `tab-${name}`));
  if (name === 'shorts') loadShorts();
  if (name === 'setup' && !Object.keys(setupState).length) refreshSetup();
  try { localStorage.setItem('tab', name); } catch { /* ignore */ }
}
document.querySelectorAll('.nav').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));

// ------------------------------------------------------------------ add

const URL_RE = /https?:\/\/[^\s"'<>]+/g;
function countLinks() {
  const n = new Set($('#links').value.match(URL_RE) || []).size;
  $('#links-count').textContent = n ? `${n} link${n > 1 ? 's' : ''}` : '';
}
$('#links').addEventListener('input', countLinks);

$('#add-links').addEventListener('click', async () => {
  const n = await sv.addLinks($('#links').value, $('#section').value, $('#prio-links').dataset.value);
  if (!n) return toast('No links found. Paste YouTube or Twitch URLs.', 'bad');
  $('#links').value = '';
  $('#section').value = '';
  countLinks();
  toast(`Added ${n} to the queue${engineNote()}`, engineDown() ? 'bad' : '');
});

const engineDown = () => ['stopped', 'error'].includes(engineState.state);
const engineNote = () => (engineDown() ? '. The engine isn\'t running, so it will wait (see Setup).' : '');

let pendingFiles = [];
function renderFiles() {
  $('#file-list').replaceChildren(...pendingFiles.map((f, i) => el('li', {},
    el('span', {}, basename(f)),
    el('button', { class: 'link', onclick: () => { pendingFiles.splice(i, 1); renderFiles(); } }, 'Remove'))));
  $('#add-files').disabled = !pendingFiles.length;
  $('#add-files').textContent = pendingFiles.length > 1 ? `Add ${pendingFiles.length} videos` : 'Add video';
}
function addPending(paths) {
  for (const p of paths) if (p && !pendingFiles.includes(p)) pendingFiles.push(p);
  renderFiles();
}
$('#pick-files').addEventListener('click', async () => addPending(await sv.pickFiles()));
const drop = $('#drop');
drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
drop.addEventListener('dragleave', () => drop.classList.remove('over'));
drop.addEventListener('drop', e => {
  e.preventDefault();
  drop.classList.remove('over');
  addPending([...e.dataTransfer.files].map(f => sv.pathForFile(f)));
});
$('#add-files').addEventListener('click', async () => {
  const n = await sv.addFiles(pendingFiles, $('#prio-files').dataset.value);
  pendingFiles = [];
  renderFiles();
  toast(n ? `Added ${n} video${n > 1 ? 's' : ''} to the queue${engineNote()}` : 'Those files are not videos',
    n && !engineDown() ? '' : 'bad');
});

// ------------------------------------------------------------------ queue

function jobOrder(a, b) {
  const rank = j => ({ running: 0, queued: 1, retry: 2 }[j.status] ?? 3);
  if (rank(a) !== rank(b)) return rank(a) - rank(b);
  if (rank(a) < 3) return PRIORITIES.indexOf(a.priority) - PRIORITIES.indexOf(b.priority) || a.seq - b.seq;
  return (b.finished || '').localeCompare(a.finished || '');
}

function stepper(job) {
  const at = STAGES.indexOf(job.stage);
  return el('ol', { class: 'stages' }, STAGES.map((s, i) => {
    let cls = '';
    if (job.status === 'done') cls = 'done';
    else if (at >= 0 && i < at) cls = 'done';
    else if (i === at) cls = job.status === 'running' ? 'now' : (job.status === 'failed' || job.status === 'retry') ? 'bad' : '';
    return el('li', { class: cls }, STAGE_LABEL[s]);
  }));
}

// An add the engine hasn't taken in yet: it becomes a real job once the engine is running.
function pendingCard(req) {
  const why = engineDown()
    ? `The engine isn't running${engineState.message ? ` (${engineState.message})` : ''}. It starts once Setup is done.`
    : 'The engine is starting and will pick this up in a moment.';
  return el('article', { class: 'job s-queued' },
    el('div', { class: 'job-head' },
      el('div', { class: 'job-title' },
        el('strong', { title: req.target }, req.kind === 'file' ? basename(req.target) : req.target),
        el('small', {}, [req.kind === 'file' ? 'Video file' : 'Link', req.section ? ` · part ${req.section}` : ''].join(''))),
      el('span', { class: 'status st-queued' }, 'Waiting for engine'),
      el('span', { class: `prio p-${req.priority}` }, PRIO_LABEL[req.priority] || '')),
    el('div', { class: 'job-foot' },
      el('span', { class: 'meta' }, why),
      el('span', { class: 'grow' }),
      el('button', { class: 'ghost sm', onclick: () => sv.removePending(req.id) }, 'Remove')));
}

function renderJobs() {
  const list = [...jobs.values()].sort(jobOrder);
  $('#jobs-empty').style.display = list.length || pending.length ? 'none' : '';
  const active = list.filter(j => ['running', 'queued', 'retry'].includes(j.status)).length + pending.length;
  $('#badge-queue').textContent = active || '';
  $('#jobs').replaceChildren(...pending.map(pendingCard), ...list.map(job => {
    const live = job.status === 'running' || job.status === 'queued' || job.status === 'retry';
    const name = job.title || (job.kind === 'file' ? basename(job.target) : job.target);
    const prio = el('select', { class: `prio p-${job.priority}`, disabled: !live && job.status !== 'done',
      onchange: e => sv.setPriority(job.id, e.target.value) },
      PRIORITIES.map(p => el('option', { value: p, selected: p === job.priority }, PRIO_LABEL[p])));
    const lines = logs.get(job.id) || [];
    return el('article', { class: `job s-${job.status}` },
      el('div', { class: 'job-head' },
        el('div', { class: 'job-title' },
          el('strong', { title: job.target }, name),
          el('small', {}, [job.kind === 'file' ? 'Video file' : 'Link', job.section ? ` · part ${job.section}` : '',
            ` · added ${ago(job.added)}`].join(''))),
        el('span', { class: `status st-${job.status}` }, STATUS_LABEL[job.status] || job.status),
        prio),
      stepper(job),
      el('div', { class: 'job-foot' },
        el('span', { class: 'meta' }, [
          job.shorts_rendered ? `${job.shorts_rendered} Short${job.shorts_rendered > 1 ? 's' : ''} rendered` : '',
          job.posts && job.posts.length ? `${job.posts.length} in OneDrive` : '',
          job.status === 'retry' && job.retry_at
            ? `retrying at ${new Date(job.retry_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : '',
        ].filter(Boolean).join(' · ')),
        job.status === 'running' && lines.length ? el('span', { class: 'live' }, lines[lines.length - 1]) : null,
        el('span', { class: 'grow' }),
        el('button', { class: 'link', onclick: () => toggleLog(job.id) }, openLogs.has(job.id) ? 'Hide log' : 'Log'),
        ['failed', 'cancelled', 'retry'].includes(job.status) ? el('button', { class: 'ghost sm', onclick: () => sv.retry(job.id) }, 'Retry now') : null,
        live ? el('button', { class: 'ghost sm', onclick: () => cancelJob(job) }, 'Cancel') : null,
        job.status !== 'running' ? el('button', { class: 'ghost sm', onclick: () => sv.remove(job.id) }, 'Remove') : null),
      job.error && job.status !== 'cancelled' ? el('p', { class: 'error' }, job.error) : null,
      openLogs.has(job.id) ? el('pre', { class: 'log', id: `log-${job.id}` }, lines.join('\n') || 'Loading…') : null);
  }));
}

async function toggleLog(id) {
  if (openLogs.has(id)) {
    openLogs.delete(id);
  } else {
    openLogs.add(id);
    const text = await sv.jobLog(id);
    if (text) logs.set(id, text.split('\n').slice(-200));
  }
  renderJobs();
  const pre = document.getElementById(`log-${id}`);
  if (pre) pre.scrollTop = pre.scrollHeight;
}

async function cancelJob(job) {
  if (job.status === 'running' && !confirm('Stop this video? Finished steps are kept; Retry continues from there.')) return;
  await sv.cancel(job.id);
}

// ------------------------------------------------------------------ shorts

const PRIO_RANK = { high: 0, normal: 1, low: 2 };
function postOrder(a, b) {
  // Same order as the n8n poster: priority, then score, then oldest export.
  const pa = a.post, pb = b.post;
  return (PRIO_RANK[pa.priority] ?? 1) - (PRIO_RANK[pb.priority] ?? 1)
    || (pb.score ?? 0) - (pa.score ?? 0)
    || String(pa.exported_at).localeCompare(String(pb.exported_at));
}

let shortsError = null;
async function loadShorts() {
  const r = await sv.listShorts();
  shorts = r.items;
  shortsError = r.error;
  renderShorts();
}
let shortsTimer;
const loadShortsSoon = () => { clearTimeout(shortsTimer); shortsTimer = setTimeout(loadShorts, 500); };

function renderShorts() {
  const counts = { uploading: 0, waiting: 0, held: 0, posted: 0, check: 0 };
  shorts.forEach(s => { counts[s.status]++; });
  for (const k of Object.keys(counts)) $(`#n-${k}`).textContent = counts[k];
  $('#badge-shorts').textContent = counts.waiting || '';
  let list = shorts.filter(s => s.status === filter);
  list = filter === 'posted'
    ? list.sort((a, b) => String(b.result?.posted_at).localeCompare(String(a.result?.posted_at)))
    : list.sort(postOrder);
  $('#shorts-error').textContent = shortsError || '';
  $('#shorts-empty').textContent = list.length ? '' : {
    uploading: 'Nothing waiting to upload. New Shorts go to your online OneDrive as soon as they are made.',
    waiting: 'No Shorts waiting. Finished videos show up here once they reach OneDrive.',
    held: 'Nothing on hold. Shorts that failed the self-check land here for review.',
    posted: 'Nothing posted yet. n8n moves Shorts to posted/ after uploading them.',
    check: 'Nothing needs a look. Shorts whose upload failed or was never confirmed show up here.',
  }[filter];
  $('#shorts').replaceChildren(...list.map((s, i) => shortCard(s, filter === 'waiting' && i === 0)));
}

function shortCard(s, nextUp) {
  const p = s.post;
  if (s.status === 'check') return checkCard(s);
  const editable = s.where === 'ready' || s.where === 'outbox';
  const title = el('input', { value: p.title || '', maxlength: 100, disabled: !editable });
  const count = el('small', { class: 'count' }, `${(p.title || '').length}/100`);
  const desc = el('textarea', { rows: 5, disabled: !editable }, p.description || '');
  const tags = el('input', { value: (p.tags || []).join(', '), disabled: !editable, placeholder: 'tag, tag, tag' });
  const prio = el('select', { class: `prio p-${p.priority || 'normal'}`, disabled: !editable },
    PRIORITIES.map(x => el('option', { value: x, selected: x === (p.priority || 'normal') }, PRIO_LABEL[x])));
  const save = el('button', { class: 'primary sm', disabled: true }, 'Save');
  const dirty = () => { save.disabled = false; count.textContent = `${title.value.length}/100`; };
  [title, desc, tags].forEach(x => x.addEventListener('input', dirty));
  prio.addEventListener('change', () => { prio.className = `prio p-${prio.value}`; dirty(); });

  const patch = extra => ({
    title: title.value, description: desc.value, priority: prio.value,
    tags: tags.value.split(',').map(t => t.trim()).filter(Boolean), ...extra,
  });
  save.addEventListener('click', async () => {
    try {
      await sv.savePost(s.id, patch());
      toast('Saved. n8n will post the new version.');
      loadShorts();
    } catch (e) { toast(String(e.message || e).replace(/^Error invoking remote method '[^']+': (Error: )?/, ''), 'bad'); }
  });
  const holdBtn = el('button', { class: 'ghost sm', onclick: async () => {
    try {
      await sv.savePost(s.id, patch({ hold: !p.hold }));
      toast(p.hold ? 'Released: n8n can post it' : 'On hold: n8n will skip it');
      loadShorts();
    } catch (e) { toast(String(e.message || e), 'bad'); }
  } }, p.hold ? 'Release' : 'Hold');

  const video = s.videoUrl
    ? el('video', { src: s.videoUrl, controls: true, preload: 'metadata' })
    : el('div', { class: 'novideo' }, 'Video not on this PC');
  const yt = s.result?.url;
  return el('article', { class: `short st-${s.status}` },
    el('div', { class: 'phone' }, video, nextUp ? el('span', { class: 'next' }, 'Next up') : null),
    el('div', { class: 'short-body' },
      el('div', { class: 'tags-row' },
        p.score != null ? el('span', { class: 'pill score' }, `${p.score}/10`) : null,
        p.category ? el('span', { class: 'pill' }, p.category) : null,
        p.creator ? el('span', { class: 'pill' }, p.creator) : null,
        el('span', { class: 'grow' }),
        prio),
      s.where === 'outbox' ? el('p', { class: 'hint' }, s.uploadError
        ? `Upload failed, retrying: ${s.uploadError}` : 'Waiting to upload to OneDrive. Edits here go up with it.') : null,
      p.flag ? el('p', { class: 'flag' }, `Self-check: ${p.flag}`) : null,
      yt ? el('p', { class: 'posted' }, 'Posted ', ago(s.result.posted_at), ' · ',
        el('button', { class: 'link', onclick: () => sv.openExternal(yt) }, yt.replace('https://', '')),
        s.result.privacy && s.result.privacy !== 'public' ? el('b', {}, ` (${s.result.privacy})`) : null) : null,
      el('label', { class: 'field' }, el('span', { class: 'lbl' }, 'Title ', count), title),
      el('label', { class: 'field' }, 'Description', desc),
      el('label', { class: 'field' }, 'Tags', tags),
      el('div', { class: 'row' },
        p.source_url ? el('button', { class: 'link', onclick: () => sv.openExternal(p.source_url) }, `Source ${p.source_timestamps || ''}`) : null,
        el('span', { class: 'grow' }),
        editable ? holdBtn : null,
        editable ? save : null)));
}

const CHECK_TEXT = {
  failed: 'The upload failed for this Short',
  unconfirmed: 'The upload was never confirmed',
  posted: 'Posted, but n8n could not move it to posted/',
};
function checkCard(s) {
  const p = s.post;
  const r = s.result || {};
  const act = action => async () => {
    try {
      await sv.resolveCheck(s.id, action);
      toast(action === 'retry' ? 'Back in the queue: n8n will try it again' : 'Moved to posted');
      loadShorts();
    } catch (e) { toast(String(e.message || e), 'bad'); }
  };
  return el('article', { class: 'short st-check' },
    el('div', { class: 'phone' }, s.videoUrl ? el('video', { src: s.videoUrl, controls: true, preload: 'metadata' })
      : el('div', { class: 'novideo' }, 'Video not on this PC')),
    el('div', { class: 'short-body' },
      el('div', { class: 'tags-row' }, el('strong', {}, p.title), el('span', { class: 'grow' }),
        p.score != null ? el('span', { class: 'pill score' }, `${p.score}/10`) : null),
      el('p', { class: 'flag' }, `${CHECK_TEXT[r.status] || 'n8n left a result for this Short'}`,
        r.kind && r.kind !== 'post' ? ` (${r.kind})` : '', r.error ? `: ${r.error}` : ''),
      r.status === 'unconfirmed' || r.status === 'posted'
        ? el('p', { class: 'hint' }, 'Look for it in YouTube Studio. If it is there, mark it posted; if not, try again.')
        : el('p', { class: 'hint' }, 'Fix the cause (for a copyright claim, usually skip this Short), then try again or leave it.'),
      s.screenshotUrl ? el('img', { class: 'shot', src: s.screenshotUrl, alt: 'Studio when the upload failed' }) : null,
      el('div', { class: 'row' },
        el('span', { class: 'grow' }),
        r.url ? el('button', { class: 'link', onclick: () => sv.openExternal(r.url) }, 'Open on YouTube') : null,
        el('button', { class: 'ghost sm', onclick: act('posted') }, 'Mark posted'),
        el('button', { class: 'primary sm', onclick: act('retry') }, 'Try again'))));
}

document.querySelectorAll('#filters .chip').forEach(c => c.addEventListener('click', () => {
  filter = c.dataset.filter;
  document.querySelectorAll('#filters .chip').forEach(x => x.classList.toggle('active', x === c));
  renderShorts();
}));
$('#refresh-shorts').addEventListener('click', loadShorts);
$('#open-output').addEventListener('click', () => sv.showFolder());

// ------------------------------------------------------------------ settings

function fillSettings(s) {
  settings = s;
  $('#s-minscore').value = s.minScore || 0;
  $('#s-maxshorts').value = s.maxShorts || 0;
  $('#s-delete').checked = !!s.deleteVideo;
  $('#s-login').checked = !!s.startAtLogin;
  seg($('#s-priority'), s.defaultPriority || 'normal');
  seg($('#prio-links'), s.defaultPriority || 'normal');
  seg($('#prio-files'), s.defaultPriority || 'normal');
}

$('#save-settings').addEventListener('click', async () => {
  const s = await sv.setSettings({
    minScore: Number($('#s-minscore').value) || 0,
    maxShorts: Number($('#s-maxshorts').value) || 0,
    defaultPriority: $('#s-priority').dataset.value,
    deleteVideo: $('#s-delete').checked,
    startAtLogin: $('#s-login').checked,
  });
  fillSettings(s);
  $('#settings-saved').textContent = 'Saved';
  setTimeout(() => { $('#settings-saved').textContent = ''; }, 2500);
});
$('#restart-engine').addEventListener('click', () => { sv.restartEngine(); toast('Restarting the engine'); });

const errText = e => String(e && e.message || e).replace(/^Error invoking remote method '[^']+': (Error: )?/, '');

// ------------------------------------------------------------------ OneDrive sign-in dialog

function showOneDriveLogin(st) {
  const modal = $('#od-modal');
  if (st.login) {
    $('#od-code').textContent = st.login.code;
    $('#od-open').dataset.url = st.login.url;
    modal.classList.add('show');
  } else {
    modal.classList.remove('show');
  }
  if (st.error) toast(st.error, 'bad');
}
$('#od-open').addEventListener('click', e => sv.openExternal(e.currentTarget.dataset.url));
$('#od-copy').addEventListener('click', () => {
  navigator.clipboard.writeText($('#od-code').textContent).then(() => toast('Code copied'));
});
$('#od-cancel').addEventListener('click', () => sv.cancelOneDrive());
sv.onOneDriveStatus(st => {
  const wasLogin = $('#od-modal').classList.contains('show');
  showOneDriveLogin(st);
  if (wasLogin && st.signedIn) { toast('OneDrive connected'); loadShorts(); checkStep('microsoft'); }
});

function showUpload(u) {
  const box = $('#upload-box');
  const busy = u.active || u.pending;
  box.classList.toggle('show', !!(busy || u.error));
  box.classList.toggle('bad', !!u.error && !u.active);
  $('#upload-state').textContent = u.active
    ? `Uploading ${u.pending > 1 ? `1 of ${u.pending}` : 'a Short'}`
    : u.pending ? `${u.pending} Short${u.pending > 1 ? 's' : ''} to upload` : 'Upload problem';
  $('#upload-msg').textContent = u.active ? `${Math.round((u.progress || 0) * 100)}%` : u.error || '';
  $('#upload-bar').style.width = `${Math.round((u.progress || 0) * 100)}%`;
  $('#n-uploading').textContent = u.pending || 0;
}
sv.onUploadStatus(showUpload);
sv.onShortsChanged(loadShortsSoon);

// ------------------------------------------------------------------ Setup tab

const setupState = {};   // step id -> last check result
const setupLogs = {};    // step id -> lines
let secretsMasked = {};

const copyBtn = (text, label = 'Copy') => el('button', { class: 'ghost sm', onclick: () =>
  navigator.clipboard.writeText(text).then(() => toast('Copied')) }, label);
const link = (url, label) => el('button', { class: 'link', onclick: () => sv.openExternal(url) }, label || url);
const val = id => { const x = document.getElementById(id); return x ? x.value.trim() : ''; };

function input(id, label, { value = '', secret, placeholder, hint, type = 'text' } = {}) {
  return el('label', { class: 'field' }, label, hint ? el('small', {}, hint) : null,
    el('input', { id, value, type: secret ? 'password' : type, placeholder: secret && value ? '' : placeholder || '',
      autocomplete: 'off', spellcheck: 'false' }));
}

async function act(action, values, { quiet } = {}) {
  if (action === 'workflow.create') {  // the workflow card's choices are saved right before Create/Update
    await sv.setupAction('save', { postEveryHours: Number(val('st-wf-hours')) || 4,
      timezone: val('st-wf-tz') || 'America/New_York', uploader: val('st-wf-up') || 'browser' });
    settings = await sv.getSettings();
  }
  const r = await sv.setupAction(action, values);
  if (!r.ok) toast(r.error, 'bad');
  else if (!quiet) toast('Done');
  return r;
}

function button(label, action, { primary, values, after, confirmText } = {}) {
  return el('button', { class: `${primary ? 'primary' : 'ghost'} sm`, onclick: async e => {
    if (confirmText && !confirm(confirmText)) return;
    const b = e.currentTarget;
    b.disabled = true;
    const old = b.textContent;
    b.textContent = 'Working…';
    try {
      const r = await act(action, typeof values === 'function' ? values() : values, { quiet: true });
      if (r.ok && r.result && r.result.message) toast(r.result.message, r.result.ok === false ? 'bad' : '');
      if (after) await after(r);
    } finally {
      b.disabled = false;
      b.textContent = old;
    }
  } }, label);
}

async function save(values, recheck) {
  await sv.setupAction('save', values);
  secretsMasked = await sv.setupSecrets();
  settings = await sv.getSettings();
  for (const s of [].concat(recheck)) await checkStep(s);
}

const STEPS = [
  { id: 'workspace', title: 'Workspace folder',
    blurb: 'Where StreamVault keeps its scripts, downloads, transcripts and Shorts. Long streams are several GB each: pick a drive with plenty of space.',
    body: st => [
      el('div', { class: 'row' }, el('code', { class: 'path' }, st.dir || settings.repoDir), el('span', { class: 'grow' }),
        button('Choose folder', 'workspace.choose', { after: () => checkStep('workspace') }),
        st.needsCopy ? button(st.installed ? 'Update scripts' : 'Copy scripts', 'workspace.copy', { primary: true, after: () => refreshSetup() }) : null),
    ] },
  { id: 'tools', title: 'Tools',
    blurb: 'Programs StreamVault uses. Install opens the official installer for each one (winget, or the vendor script); Windows may ask for permission.',
    body: st => [
      el('table', { class: 'tools' }, Object.entries(st.tools || {}).map(([id, t]) => el('tr', {},
        el('td', {}, el('span', { class: `dot2 ${t.ok ? 'ok' : 'bad'}` }), el('strong', {}, t.name), el('small', {}, t.why)),
        el('td', { class: 'msg' }, t.message),
        el('td', {}, t.ok ? null : button('Install', 'tools.install', { primary: true, values: { id }, after: () => checkStep('tools') }))))),
      el('p', { class: 'hint' }, 'VoiceStudio has to be running for its check to pass: start it from the Start menu after installing.'),
    ] },
  { id: 'python', title: 'Python environment',
    blurb: 'Installs the Python packages the scripts need (uv sync) and links the video-use skill for Claude Code. A few minutes the first time.',
    body: () => [el('div', { class: 'row' }, el('span', { class: 'grow' }),
      button('Set up Python environment', 'python.sync', { primary: true, after: () => checkStep('python') }))] },
  { id: 'claude', title: 'Claude sign-in',
    blurb: 'Claude Code picks the best moments and renders each Short, using your Claude subscription.',
    body: () => [el('div', { class: 'row' }, el('span', { class: 'hint' }, 'Opens a window that signs you in through the browser. Come back and press Re-check when it says you’re logged in.'),
      el('span', { class: 'grow' }), button('Sign in to Claude', 'claude.login', { primary: true }))] },
  { id: 'voicestudio', title: 'VoiceStudio model',
    blurb: 'Free transcription on your GPU.',
    body: () => [
      el('ol', { class: 'steps' },
        el('li', {}, 'Open VoiceStudio → ', el('b', {}, 'Model Catalogue → ASR'), ', choose ', el('b', {}, 'WhisperX (large-v3)'), ' and let it download.'),
        el('li', {}, 'Make sure it runs on ', el('b', {}, 'CUDA (float16)'), ', not CPU.'),
        el('li', {}, 'Allow long files, then restart VoiceStudio: ', button('Allow 1-hour files', 'voicestudio.timeout'))),
      el('div', { class: 'row' }, input('st-vs-url', 'VoiceStudio address', { value: settings.voicestudioUrl }), el('span', { class: 'grow' }),
        el('button', { class: 'ghost sm', onclick: () => save({ voicestudioUrl: val('st-vs-url') || 'http://127.0.0.1:3900' }, ['voicestudio', 'tools']) }, 'Save'),
        button('Test transcription', 'voicestudio.test', { primary: true })),
    ] },
  { id: 'n8n', title: 'n8n server',
    blurb: 'Where the poster runs. The app creates and updates the workflow there through n8n’s API.',
    body: st => [
      el('div', { class: 'row wrap' },
        input('st-n8n-url', 'n8n address', { value: settings.n8nUrl, placeholder: 'https://n8n.example.com' }),
        input('st-n8n-key', 'API key', { secret: true, value: secretsMasked.n8nApiKey, placeholder: secretsMasked.n8nApiKey || 'n8n → Settings → n8n API → Create an API key' })),
      el('div', { class: 'row' }, el('span', { class: 'grow' }),
        el('button', { class: 'primary sm', onclick: () => save({ n8nUrl: val('st-n8n-url').replace(/\/+$/, ''), n8nApiKey: val('st-n8n-key') }, ['n8n', 'microsoft']) }, 'Save and test')),
      st.redirectUrl ? el('p', { class: 'hint' }, 'OAuth redirect URL (used in the next steps): ', el('code', {}, st.redirectUrl), ' ', copyBtn(st.redirectUrl)) : null,
    ] },
  { id: 'microsoft', title: 'Microsoft account (OneDrive)',
    blurb: 'One Azure app lets both this app and n8n use your OneDrive. Register it once:',
    body: st => {
      const redirect = setupState.n8n && setupState.n8n.redirectUrl;
      return [
        el('ol', { class: 'steps' },
          el('li', {}, 'Open ', link('https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade', 'App registrations'), ' → ', el('b', {}, 'New registration'), '. Name: StreamVault.'),
          el('li', {}, 'Supported account types: ', el('b', {}, 'any organizational directory and personal Microsoft accounts'), '.'),
          el('li', {}, 'Redirect URI: ', el('b', {}, 'Web'), ', ', redirect ? el('code', {}, redirect) : el('i', {}, '(finish the n8n step first)'), ' ', redirect ? copyBtn(redirect) : null, '. Register.'),
          el('li', {}, el('b', {}, 'Authentication'), ' → Advanced settings → ', el('b', {}, 'Allow public client flows: Yes'), ' → Save.'),
          el('li', {}, el('b', {}, 'Certificates & secrets'), ' → New client secret. Copy its ', el('b', {}, 'Value'), ' below, with the Application (client) ID from Overview.')),
        el('div', { class: 'row wrap' },
          input('st-ms-id', 'Application (client) ID', { value: settings.msClientId, placeholder: '00000000-0000-0000-0000-000000000000' }),
          input('st-ms-secret', 'Client secret value', { secret: true, placeholder: secretsMasked.msClientSecret || 'paste the secret value' }),
          input('st-od-folder', 'Folder in OneDrive', { value: settings.onedriveFolder || 'StreamVault' })),
        el('div', { class: 'row' },
          st.signedIn ? el('span', { class: 'hint' }, `Signed in as ${st.account ? st.account.email : 'your account'}`) : null,
          el('span', { class: 'grow' }),
          el('button', { class: 'ghost sm', onclick: () => save({ msClientId: val('st-ms-id'), msClientSecret: val('st-ms-secret'),
            onedriveFolder: val('st-od-folder').replace(/^\/+|\/+$/g, '') || 'StreamVault' }, 'microsoft') }, 'Save'),
          st.signedIn ? button('Disconnect', 'microsoft.disconnect', { confirmText: 'Disconnect OneDrive? New Shorts wait on this PC until you connect again.', after: () => checkStep('microsoft') }) : null,
          el('button', { class: 'primary sm', onclick: async () => {
            await save({ msClientId: val('st-ms-id'), msClientSecret: val('st-ms-secret'),
              onedriveFolder: val('st-od-folder').replace(/^\/+|\/+$/g, '') || 'StreamVault' }, []);
            try { showOneDriveLogin(await sv.connectOneDrive()); } catch (e) { toast(errText(e), 'bad'); }
          } }, st.signedIn ? 'Reconnect OneDrive' : 'Connect OneDrive')),
      ];
    } },
  { id: 'github', title: 'GitHub + YouTube sign-in (Studio uploader)',
    blurb: 'Until Google audits your API project, n8n posts by starting a GitHub Actions run that uploads through YouTube Studio with your saved sign-in. Automating Studio is against YouTube’s terms; the channel can be flagged.',
    body: st => [
      el('ol', { class: 'steps' },
        el('li', {}, link('https://github.com/settings/personal-access-tokens/new', 'Create a fine-grained token'), ': only the ', el('b', {}, settings.githubRepo), ' repository; permissions ',
          el('b', {}, 'Actions: Read and write'), ' and ', el('b', {}, 'Secrets: Read and write'), '.'),
        el('li', {}, 'Save it here, then open Chrome, sign in to YouTube Studio with the channel’s account, and save the session.')),
      el('div', { class: 'row wrap' },
        input('st-gh-repo', 'Repository', { value: settings.githubRepo, placeholder: 'owner/name' }),
        input('st-gh-token', 'Token', { secret: true, placeholder: secretsMasked.githubToken || 'github_pat_…' })),
      el('div', { class: 'row' }, el('span', { class: 'grow' }),
        el('button', { class: 'ghost sm', onclick: () => save({ githubRepo: val('st-gh-repo'), githubToken: val('st-gh-token') }, 'github') }, 'Save and check'),
        button('1. Open Chrome', 'github.openChrome'),
        button('2. Save YouTube session', 'github.capture', { primary: true, after: () => checkStep('github') })),
    ] },
  { id: 'youtubeApi', title: 'YouTube API (optional)', optional: true,
    blurb: 'For after Google approves your API project’s audit; then n8n uploads directly, with no browser. Skip it for now if you like.',
    body: () => {
      const redirect = setupState.n8n && setupState.n8n.redirectUrl;
      return [
        el('ol', { class: 'steps' },
          el('li', {}, 'In ', link('https://console.cloud.google.com/apis/library/youtube.googleapis.com', 'Google Cloud'), ', enable ', el('b', {}, 'YouTube Data API v3'), '.'),
          el('li', {}, 'OAuth consent screen: External, then Publishing status ', el('b', {}, 'In production'), ' (in Testing, sign-ins expire after 7 days).'),
          el('li', {}, 'Credentials → OAuth client → ', el('b', {}, 'Web application'), ', redirect URI ', redirect ? el('code', {}, redirect) : el('i', {}, '(n8n step)'), ' ', redirect ? copyBtn(redirect) : null, '.'),
          el('li', {}, 'Submit the ', link('https://support.google.com/youtube/contact/yt_api_form', 'API audit'), '; until it’s approved, API uploads stay private.')),
        el('div', { class: 'row wrap' },
          input('st-g-id', 'Client ID', { value: settings.googleClientId }),
          input('st-g-secret', 'Client secret', { secret: true, placeholder: secretsMasked.googleClientSecret || '' })),
        el('div', { class: 'row' }, el('span', { class: 'grow' }),
          el('button', { class: 'ghost sm', onclick: () => save({ googleClientId: val('st-g-id'), googleClientSecret: val('st-g-secret') }, ['youtubeApi', 'workflow']) }, 'Save')),
      ];
    } },
  { id: 'workflow', title: 'n8n posting workflow',
    blurb: 'Creates the credentials and the "StreamVault: post Shorts" workflow on your n8n from the built-in template, or updates them after you change something above.',
    body: st => {
      const ytSet = setupState.youtubeApi && setupState.youtubeApi.set;
      const hours = [1, 2, 3, 4, 6, 8, 12, 24];
      const creds = st.credentials || {};
      return [
        el('div', { class: 'row wrap' },
          el('label', { class: 'field' }, 'Post every', el('select', { id: 'st-wf-hours' },
            hours.map(h => el('option', { value: h, selected: Number(settings.postEveryHours) === h }, h === 24 ? 'day' : `${h} hour${h > 1 ? 's' : ''}`)))),
          input('st-wf-tz', 'Time zone', { value: settings.timezone, hint: 'the audience’s, e.g. America/New_York' }),
          el('label', { class: 'field' }, 'Upload through', el('select', { id: 'st-wf-up' },
            el('option', { value: 'browser', selected: settings.uploader !== 'api' }, 'YouTube Studio (GitHub Actions)'),
            el('option', { value: 'api', selected: settings.uploader === 'api', disabled: !ytSet }, 'YouTube API (after the audit)')))),
        el('div', { class: 'row' }, el('span', { class: 'grow' }),
          button(st.exists ? 'Update workflow in n8n' : 'Create workflow in n8n', 'workflow.create',
            { primary: !st.exists, after: () => checkStep('workflow') }),
          st.exists && !st.active ? button('Activate', 'workflow.activate', { primary: true, after: () => checkStep('workflow') }) : null,
          st.url ? el('button', { class: 'ghost sm', onclick: () => sv.openExternal(st.url) }, 'Open in n8n') : null),
        Object.keys(creds).length ? el('div', { class: 'creds' }, el('span', { class: 'hint' }, 'In n8n, open each sign-in credential and click Connect, once:'),
          ['onedrive', 'youtube'].filter(k => creds[k]).map(k => el('button', { class: 'ghost sm', onclick: () => sv.openExternal(creds[k].url) },
            `Connect ${k === 'onedrive' ? 'OneDrive' : 'YouTube'} in n8n`))) : null,
        st.executions && st.executions.length ? el('table', { class: 'execs' },
          el('tr', {}, el('th', {}, 'Recent runs'), el('th', {}, '')),
          st.executions.map(x => el('tr', {}, el('td', {}, new Date(x.startedAt).toLocaleString()),
            el('td', { class: `st-${x.status === 'success' ? 'done' : x.status === 'error' ? 'failed' : 'queued'}` }, x.status)))) : null,
      ];
    } },
];

function renderStep(step, i) {
  const st = setupState[step.id] || { message: 'Checking…' };
  const card = document.getElementById(`step-${step.id}`) || el('article', { class: 'step', id: `step-${step.id}` });
  const status = st.checking ? 'Checking…' : st.ok ? (step.optional && !st.set ? 'Skipped' : 'Done') : step.optional ? 'Optional' : 'To do';
  const lines = setupLogs[step.id] || [];
  card.className = `step ${st.ok ? 'ok' : 'todo'}`;
  card.replaceChildren(
    el('div', { class: 'step-head' },
      el('span', { class: 'num' }, st.ok ? '✓' : String(i + 1)),
      el('div', { class: 'step-title' }, el('h2', {}, step.title), el('small', {}, st.message || '')),
      el('span', { class: `pill ${st.ok ? 'ok' : step.optional ? '' : 'bad'}` }, status),
      el('button', { class: 'link', onclick: () => checkStep(step.id) }, 'Re-check')),
    el('p', { class: 'blurb' }, step.blurb),
    ...[...step.body(st),
      lines.length ? el('pre', { class: 'log', id: `setup-log-${step.id}` }, lines.join('\n')) : null].filter(Boolean));
  return card;
}

// Re-render one card (or all). Re-rendering only the card that changed keeps what you typed elsewhere.
function renderSetup(onlyId) {
  const box = $('#steps');
  STEPS.forEach((s, i) => {
    if (onlyId && s.id !== onlyId && document.getElementById(`step-${s.id}`)) return;
    const c = renderStep(s, i);
    if (!c.parentNode) box.append(c);
  });
  const todo = STEPS.filter(s => !s.optional && !(setupState[s.id] && setupState[s.id].ok)).length;
  $('#badge-setup').textContent = todo || '';
  const pre = box.querySelectorAll('pre.log');
  pre.forEach(p => { p.scrollTop = p.scrollHeight; });
  return todo;
}

async function checkStep(id) {
  setupState[id] = { ...(setupState[id] || {}), checking: true };
  renderSetup(id);
  const r = await sv.setupCheck(id);
  setupState[id] = id === 'tools'
    ? { tools: r, ok: Object.values(r).every(t => t.ok || t.optional), message: Object.values(r).filter(t => !t.ok).length
      ? `${Object.values(r).filter(t => !t.ok).length} missing: ${Object.values(r).filter(t => !t.ok).map(t => t.name).join(', ')}` : 'all installed' }
    : r;
  renderSetup(id);
  // Steps that show values from another step's check.
  if (id === 'n8n') { renderSetup('microsoft'); renderSetup('youtubeApi'); }
  if (id === 'youtubeApi') renderSetup('workflow');
  return setupState[id];
}

async function refreshSetup() {
  secretsMasked = await sv.setupSecrets();
  settings = await sv.getSettings();
  for (const s of STEPS) await checkStep(s.id);
}

sv.onSetupLog(({ step, line }) => {
  const arr = setupLogs[step] || (setupLogs[step] = []);
  arr.push(line);
  if (arr.length > 300) arr.shift();
  const pre = document.getElementById(`setup-log-${step}`);
  if (pre) { pre.textContent = arr.join('\n'); pre.scrollTop = pre.scrollHeight; } else renderSetup(step);
});

// ------------------------------------------------------------------ engine status + events

const ENGINE_LABEL = {
  starting: 'Starting…', running: 'Ready', idle: 'Waiting for videos', working: 'Working', stopped: 'Stopped', error: 'Error',
};
function setEngine({ state, message }) {
  engineState = { state, message };
  if (pending.length) scheduleRender();
  const box = $('#engine');
  box.className = `engine e-${state}`;
  $('#engine-state').textContent = ENGINE_LABEL[state] || state;
  $('#engine-msg').textContent = message || '';
}

function appendEngineLog(line) {
  const pre = $('#engine-log');
  pre.textContent = (pre.textContent + '\n' + line).split('\n').slice(-300).join('\n').trim();
  pre.scrollTop = pre.scrollHeight;
}

let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; renderJobs(); });
}

sv.onEngineStatus(setEngine);
sv.onEngineLog(appendEngineLog);
sv.onPending(list => { pending = list; scheduleRender(); });
sv.onEvent(ev => {
  if (ev.type === 'ready') { jobs.clear(); ev.jobs.forEach(j => jobs.set(j.id, j)); }
  if (ev.type === 'job') {
    const before = jobs.get(ev.job.id);
    jobs.set(ev.job.id, ev.job);
    if (ev.job.status === 'done' && before && before.status !== 'done') {
      toast(`Done: ${ev.job.title || ev.job.target}`);
      loadShorts();
    }
  }
  if (ev.type === 'removed') jobs.delete(ev.id);
  if (ev.type === 'log') {
    const arr = logs.get(ev.id) || [];
    arr.push(ev.line);
    if (arr.length > 200) arr.shift();
    logs.set(ev.id, arr);
    const pre = document.getElementById(`log-${ev.id}`);
    if (pre) { pre.textContent = arr.join('\n'); pre.scrollTop = pre.scrollHeight; }
    if (jobs.get(ev.id)?.status === 'running') scheduleRender();
    return;
  }
  scheduleRender();
});

(async function init() {
  fillSettings(await sv.getSettings());
  const { state, pending: waiting, engine, engineLog, upload } = await sv.getState();
  Object.values(state.jobs || {}).forEach(j => jobs.set(j.id, j));
  pending = waiting || [];
  setEngine(engine);
  showUpload(upload);
  showOneDriveLogin(await sv.oneDriveStatus());
  engineLog.forEach(appendEngineLog);
  renderJobs();
  loadShorts();
  // Until setup is complete the app opens on Setup.
  secretsMasked = await sv.setupSecrets();
  STEPS.forEach(s => { setupState[s.id] = { checking: true }; });
  renderSetup();
  let tab = 'add';
  try { tab = localStorage.getItem('tab') || 'add'; } catch { /* ignore */ }
  showTab(tab);
  await refreshSetup();
  if (renderSetup() > 0 && tab !== 'setup') showTab('setup');
})();
