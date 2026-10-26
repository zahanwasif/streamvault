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
  if (name === 'settings') runChecks();
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
  toast(`Added ${n} to the queue`);
});

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
  toast(n ? `Added ${n} video${n > 1 ? 's' : ''} to the queue` : 'Those files are not videos', n ? '' : 'bad');
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

function renderJobs() {
  const list = [...jobs.values()].sort(jobOrder);
  $('#jobs-empty').style.display = list.length ? 'none' : '';
  const active = list.filter(j => ['running', 'queued', 'retry'].includes(j.status)).length;
  $('#badge-queue').textContent = active || '';
  $('#jobs').replaceChildren(...list.map(job => {
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

async function loadShorts() {
  shorts = await sv.listShorts();
  renderShorts();
}

function renderShorts() {
  const counts = { waiting: 0, held: 0, posted: 0 };
  shorts.forEach(s => { counts[s.status]++; });
  for (const k of Object.keys(counts)) $(`#n-${k}`).textContent = counts[k];
  $('#badge-shorts').textContent = counts.waiting || '';
  let list = shorts.filter(s => s.status === filter);
  list = filter === 'posted'
    ? list.sort((a, b) => String(b.result?.posted_at).localeCompare(String(a.result?.posted_at)))
    : list.sort(postOrder);
  $('#shorts-empty').textContent = list.length ? '' : {
    waiting: 'No Shorts waiting. Finished videos show up here once they reach OneDrive.',
    held: 'Nothing on hold. Shorts that failed the self-check land here for review.',
    posted: 'Nothing posted yet. n8n moves Shorts to posted/ after uploading them.',
  }[filter];
  $('#shorts').replaceChildren(...list.map((s, i) => shortCard(s, filter === 'waiting' && i === 0)));
}

function shortCard(s, nextUp) {
  const p = s.post;
  const editable = s.where === 'ready';
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

document.querySelectorAll('#filters .chip').forEach(c => c.addEventListener('click', () => {
  filter = c.dataset.filter;
  document.querySelectorAll('#filters .chip').forEach(x => x.classList.toggle('active', x === c));
  renderShorts();
}));
$('#refresh-shorts').addEventListener('click', loadShorts);
$('#open-output').addEventListener('click', () => sv.showFolder('output'));

// ------------------------------------------------------------------ settings

function fillSettings(s) {
  settings = s;
  $('#s-output').value = s.outputDir || '';
  $('#s-repo').value = s.repoDir || '';
  $('#s-voicestudio').value = s.voicestudioUrl || '';
  $('#s-minscore').value = s.minScore || 0;
  $('#s-maxshorts').value = s.maxShorts || 0;
  $('#s-delete').checked = !!s.deleteVideo;
  $('#s-login').checked = !!s.startAtLogin;
  seg($('#s-priority'), s.defaultPriority || 'normal');
  seg($('#prio-links'), s.defaultPriority || 'normal');
  seg($('#prio-files'), s.defaultPriority || 'normal');
}

document.querySelectorAll('[data-pick]').forEach(b => b.addEventListener('click', async () => {
  const dir = await sv.pickFolder();
  if (dir) $(`#${b.dataset.pick}`).value = dir;
}));

$('#save-settings').addEventListener('click', async () => {
  const s = await sv.setSettings({
    outputDir: $('#s-output').value.trim(),
    repoDir: $('#s-repo').value.trim(),
    voicestudioUrl: $('#s-voicestudio').value.trim() || 'http://127.0.0.1:3900',
    minScore: Number($('#s-minscore').value) || 0,
    maxShorts: Number($('#s-maxshorts').value) || 0,
    defaultPriority: $('#s-priority').dataset.value,
    deleteVideo: $('#s-delete').checked,
    startAtLogin: $('#s-login').checked,
  });
  fillSettings(s);
  $('#settings-saved').textContent = 'Saved';
  setTimeout(() => { $('#settings-saved').textContent = ''; }, 2500);
  runChecks();
});

const CHECK_LABEL = { voicestudio: 'VoiceStudio', claude: 'Claude Code', uv: 'uv', repo: 'StreamVault folder', output: 'Output folder' };
async function runChecks() {
  $('#checks').replaceChildren(el('dt', {}, 'Checking…'));
  const r = await sv.runChecks();
  $('#checks').replaceChildren(...Object.entries(r).flatMap(([k, v]) => [
    el('dt', { class: v.ok ? 'ok' : 'bad' }, CHECK_LABEL[k] || k), el('dd', {}, v.message)]));
}
$('#run-checks').addEventListener('click', runChecks);
$('#restart-engine').addEventListener('click', () => { sv.restartEngine(); toast('Restarting the engine'); });

// ------------------------------------------------------------------ engine status + events

const ENGINE_LABEL = {
  starting: 'Starting…', running: 'Ready', idle: 'Waiting for videos', working: 'Working', stopped: 'Stopped', error: 'Error',
};
function setEngine({ state, message }) {
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
  const { state, engine, engineLog } = await sv.getState();
  Object.values(state.jobs || {}).forEach(j => jobs.set(j.id, j));
  setEngine(engine);
  engineLog.forEach(appendEngineLog);
  renderJobs();
  loadShorts();
  let tab = 'add';
  try { tab = localStorage.getItem('tab') || 'add'; } catch { /* ignore */ }
  showTab(tab);
})();
