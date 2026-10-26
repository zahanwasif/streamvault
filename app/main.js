// StreamVault desktop app: main process.
//
// Runs the Python engine (clipper/run_queue.py --watch) as a child process and talks to it through
// files in <repo>/.worker/: the app writes requests/*.json, the engine writes state.json and prints
// `EVENT {json}` lines. Finished Shorts land in <repo>/outbox/; the uploader here sends each one to
// online OneDrive (StreamVault/ready/<post id>/), which is all the n8n poster ever reads. The Shorts
// tab reads and edits those online posts directly.

const { app, BrowserWindow, Tray, Menu, ipcMain, dialog, shell, protocol, net, nativeImage, safeStorage } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const readline = require('readline');
const { pathToFileURL } = require('url');
const { OneDrive } = require('./onedrive');
const { Workspace } = require('./workspace');
const { Secrets } = require('./secrets');
const setup = require('./setup');
const { GitHub } = require('./github');
const { N8n, buildWorkflow } = require('./n8n');

const VIDEO_EXTS = ['mp4', 'mov', 'mkv', 'avi', 'm4v', 'webm'];
const PRIORITIES = ['high', 'normal', 'low'];
const LOCKED_EXIT = 3; // run_queue.py: another engine holds the lock
const UPLOAD_BACKOFF_MIN = [1, 2, 5, 15];

protocol.registerSchemesAsPrivileged([
  { scheme: 'media', privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true } },
]);

let win = null;
let tray = null;
let engine = null;
let engineStatus = { state: 'stopped', message: '' };
let quitting = false;
let restartTimer = null;
const engineLog = [];

// ------------------------------------------------------------------ settings

const settingsFile = () => path.join(app.getPath('userData'), 'settings.json');

function defaultSettings() {
  return {
    repoDir: app.isPackaged ? 'C:\\StreamVault' : path.resolve(__dirname, '..'),
    msClientId: '',
    onedriveFolder: 'StreamVault',
    n8nUrl: 'https://n8n.veleads.io',
    githubRepo: 'zahanwasif/streamvault',
    googleClientId: '',
    postEveryHours: 4,
    timezone: 'America/New_York',
    uploader: 'browser',
    n8nIds: {},        // ids of what the app created in n8n: { onedrive, github, youtube, workflow }
    sessionSavedAt: null,
    voicestudioUrl: 'http://127.0.0.1:3900',
    defaultPriority: 'normal',
    minScore: 0,
    maxShorts: 0,
    deleteVideo: false,
    startAtLogin: false,
  };
}

let settings = null;
function loadSettings() {
  try {
    settings = { ...defaultSettings(), ...JSON.parse(fs.readFileSync(settingsFile(), 'utf8')) };
  } catch {
    settings = defaultSettings();
  }
  return settings;
}

const ENGINE_KEYS = ['repoDir', 'voicestudioUrl', 'minScore', 'maxShorts', 'deleteVideo'];
function saveSettings(next) {
  const before = { ...settings };
  settings = { ...settings, ...next };
  writeJsonAtomic(settingsFile(), settings);
  app.setLoginItemSettings({ openAtLogin: !!settings.startAtLogin, args: ['--hidden'] });
  if (before.msClientId !== settings.msClientId || before.onedriveFolder !== settings.onedriveFolder) {
    drive = makeDrive();
    sendOneDriveStatus();
  }
  if (ENGINE_KEYS.some(k => before[k] !== settings[k])) restartEngine();
  return settings;
}

// ------------------------------------------------------------------ files

const workerDir = () => path.join(settings.repoDir, '.worker');
const outboxDir = () => path.join(settings.repoDir, 'outbox');

function writeJsonAtomic(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf8');
  fs.renameSync(tmp, file);
}

function readJson(file, fallback = null) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return fallback;
  }
}

let reqCounter = 0;
function sendRequest(req) {
  // Names sort in creation order; the engine applies them in that order.
  const name = `${Date.now()}-${String(++reqCounter).padStart(4, '0')}.json`;
  writeJsonAtomic(path.join(workerDir(), 'requests', name), req);
}

// Adds the engine hasn't picked up yet (it isn't running, or is starting): the Queue lists them as waiting.
function pendingAdds() {
  const dir = path.join(workerDir(), 'requests');
  let names = [];
  try {
    names = fs.readdirSync(dir).filter(n => n.endsWith('.json')).sort();
  } catch {
    return [];
  }
  return names
    .map(name => ({ ...readJson(path.join(dir, name), {}), id: name }))
    .filter(r => r.op === 'add' && r.target);
}

function sendPending() {
  emitToUi('pending', pendingAdds());
}

function isInside(child, parent) {
  if (!parent) return false;
  const rel = path.relative(path.resolve(parent), path.resolve(child));
  return rel && !rel.startsWith('..') && !path.isAbsolute(rel);
}

function emitToUi(channel, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

// ------------------------------------------------------------------ OneDrive sign-in

// The refresh token is as good as a password for the drive: encrypt it with the OS keychain
// (DPAPI on Windows). Without one (some Linux setups) it is kept readable by this user only.
const tokenFile = () => path.join(app.getPath('userData'), 'onedrive-token.bin');
const tokenStore = {
  load() {
    try {
      const raw = fs.readFileSync(tokenFile());
      const text = raw.subarray(0, 4).toString() === 'TXT:' ? raw.subarray(4).toString()
        : safeStorage.decryptString(raw);
      return JSON.parse(text);
    } catch {
      return null;
    }
  },
  save(tok) {
    const text = JSON.stringify(tok);
    const data = safeStorage.isEncryptionAvailable() ? safeStorage.encryptString(text) : Buffer.from(`TXT:${text}`);
    fs.mkdirSync(path.dirname(tokenFile()), { recursive: true });
    fs.writeFileSync(tokenFile(), data, { mode: 0o600 });
  },
  clear() {
    fs.rmSync(tokenFile(), { force: true });
  },
};

let drive = null;
let account = null;   // { name, email } once known
let login = null;     // device-code sign-in in progress

function makeDrive() {
  return new OneDrive({ clientId: settings.msClientId, folder: settings.onedriveFolder, store: tokenStore });
}

function oneDriveStatus() {
  return {
    configured: !!settings.msClientId,
    signedIn: !!drive && drive.signedIn,
    account,
    folder: settings.onedriveFolder,
    login: login && { code: login.user_code, url: login.verification_uri, message: login.message },
  };
}

function sendOneDriveStatus(extra = {}) {
  emitToUi('onedrive-status', { ...oneDriveStatus(), ...extra });
}

async function connectOneDrive() {
  if (login) return oneDriveStatus();
  drive = makeDrive();
  const dc = await drive.startDeviceLogin();
  login = dc;
  const mine = dc;
  drive.pollDeviceLogin(dc, () => login !== mine)
    .then(me => {
      account = me;
      login = null;
      sendOneDriveStatus();
      uploadSoon(0);
    })
    .catch(e => {
      if (login === mine) login = null;
      sendOneDriveStatus({ error: e.message });
    });
  return oneDriveStatus();
}

function disconnectOneDrive() {
  login = null;
  if (drive) drive.signOut();
  account = null;
  sendOneDriveStatus();
}

// ------------------------------------------------------------------ uploader: outbox → online

const upload = { active: null, progress: 0, pending: 0, error: null, lastDone: null };
const failures = new Map(); // post id -> { count, until, error }
let uploading = false;
let uploadTimer = null;

function outboxPosts() {
  let names = [];
  try {
    names = fs.readdirSync(outboxDir(), { withFileTypes: true }).filter(d => d.isDirectory()).map(d => d.name).sort();
  } catch {
    return [];
  }
  // post.json is written last by export_posts.py: without it the folder is still being filled.
  return names.filter(n => fs.existsSync(path.join(outboxDir(), n, 'post.json')));
}

function sendUploadStatus() {
  emitToUi('upload-status', { ...upload, signedIn: !!drive && drive.signedIn });
}

function uploadSoon(ms = 1000) {
  clearTimeout(uploadTimer);
  uploadTimer = setTimeout(uploadTick, ms);
}

async function uploadTick() {
  clearTimeout(uploadTimer);
  uploadTimer = setTimeout(uploadTick, 10000);
  if (uploading) return;
  const ids = outboxPosts();
  upload.pending = ids.length;
  if (!ids.length || !drive || !drive.signedIn) {
    upload.error = ids.length && !(drive && drive.signedIn) ? 'Connect OneDrive in Settings to upload' : null;
    return sendUploadStatus();
  }
  uploading = true;
  try {
    for (const id of ids) {
      const f = failures.get(id);
      if (f && f.until > Date.now()) continue;
      upload.active = id;
      upload.progress = 0;
      upload.error = null;
      sendUploadStatus();
      const dir = path.join(outboxDir(), id);
      try {
        await drive.uploadPost(dir, p => { upload.progress = p; sendUploadStatus(); });
        fs.rmSync(dir, { recursive: true, force: true });
        failures.delete(id);
        upload.lastDone = id;
        emitToUi('shorts-changed', { id });
      } catch (e) {
        const n = (f ? f.count : 0) + 1;
        const wait = UPLOAD_BACKOFF_MIN[Math.min(n, UPLOAD_BACKOFF_MIN.length) - 1];
        failures.set(id, { count: n, until: Date.now() + wait * 60000, error: e.message });
        upload.error = `${id}: ${e.message} (retrying in ${wait} min)`;
        if (e.code === 'NOT_SIGNED_IN') {
          account = null;
          sendOneDriveStatus();
          break;
        }
      }
      upload.pending = outboxPosts().length;
    }
  } finally {
    uploading = false;
    upload.active = null;
    upload.pending = outboxPosts().length;
    sendUploadStatus();
  }
}

// ------------------------------------------------------------------ engine

function setEngineStatus(state, message = '') {
  engineStatus = { state, message };
  emitToUi('engine-status', engineStatus);
  if (tray) tray.setToolTip(`StreamVault: ${state}${message ? ` (${message})` : ''}`);
}

function startEngine() {
  clearTimeout(restartTimer);
  if (engine) return;
  if (pythonStale) {
    setEngineStatus('stopped', 'Finish Setup: the Python environment needs updating');
    return;
  }
  if (!fs.existsSync(path.join(settings.repoDir, 'clipper', 'run_queue.py'))) {
    setEngineStatus('stopped', 'Finish Setup: the scripts aren\'t in the workspace yet');
    return;
  }
  const args = ['run', '--project', 'video-use', 'python', 'clipper/run_queue.py', '--watch', '--parent-pipe',
    '--outbox', outboxDir()];
  if (settings.deleteVideo) args.push('--delete-video');
  if (Number(settings.minScore) > 0) args.push('--min-score', String(settings.minScore));
  if (Number(settings.maxShorts) > 0) args.push('--max-shorts', String(settings.maxShorts));

  const proc = spawn(process.env.STREAMVAULT_UV || 'uv', args, {
    cwd: settings.repoDir,
    env: { ...process.env, PYTHONUTF8: '1', PYTHONUNBUFFERED: '1', VOICESTUDIO_URL: settings.voicestudioUrl },
    windowsHide: true,
    detached: process.platform !== 'win32', // own process group, so the whole tree can be stopped
  });
  engine = proc;
  setEngineStatus('starting');

  readline.createInterface({ input: proc.stdout }).on('line', line => {
    if (!line.startsWith('EVENT ')) return logEngine(line);
    let ev;
    try {
      ev = JSON.parse(line.slice(6));
    } catch {
      return logEngine(line);
    }
    if (ev.type === 'ready' || ev.type === 'idle') setEngineStatus(ev.type === 'idle' ? 'idle' : 'running');
    if (ev.type === 'job' && ev.job.status === 'running') setEngineStatus('working', ev.job.title || ev.job.target);
    if (ev.type === 'job' && ev.job.posts && ev.job.posts.length) uploadSoon();
    emitToUi('engine-event', ev);
    if (ev.type === 'ready' || ev.type === 'applied') sendPending();
  });
  readline.createInterface({ input: proc.stderr }).on('line', logEngine);

  proc.on('error', err => {
    logEngine(`could not start the engine: ${err.message}`);
    setEngineStatus('error', err.code === 'ENOENT' ? 'uv not found on PATH' : err.message);
  });
  proc.on('exit', code => {
    if (engine === proc) engine = null;
    if (quitting) return;
    if (code === LOCKED_EXIT) {
      // An engine from an earlier session (the app crashed) still holds the lock: stop it, take over.
      killStaleEngine();
      restartTimer = setTimeout(startEngine, 1500);
      return;
    }
    if (engineStatus.state !== 'error') setEngineStatus('stopped', code ? `engine exited (${code})` : '');
    restartTimer = setTimeout(startEngine, 5000);
  });
}

function logEngine(line) {
  engineLog.push(line);
  if (engineLog.length > 400) engineLog.shift();
  emitToUi('engine-log', line);
}

function forceKill(pid, group) {
  if (process.platform === 'win32') {
    spawn('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true });
  } else {
    try { process.kill(group ? -pid : pid, 'SIGKILL'); } catch { /* already gone */ }
  }
}

function killStaleEngine() {
  const pid = Number(fs.readFileSync(path.join(workerDir(), 'engine.pid'), { encoding: 'utf8', flag: 'a+' }));
  if (pid) {
    logEngine(`stopping a leftover engine (pid ${pid})`);
    forceKill(pid, false);
  }
}

function killTree(proc) {
  // Closing stdin asks the engine (--parent-pipe) to stop its current step and exit; force it after 5 s.
  return new Promise(resolve => {
    if (!proc || proc.exitCode !== null || proc.signalCode) return resolve();
    const timer = setTimeout(() => forceKill(proc.pid, true), 5000);
    proc.once('exit', () => { clearTimeout(timer); resolve(); });
    proc.stdin.end();
  });
}

async function stopEngine() {
  clearTimeout(restartTimer);
  const proc = engine;
  engine = null;
  await killTree(proc);
}

async function restartEngine() {
  await stopEngine();
  startEngine();
}

function readState() {
  return readJson(path.join(workerDir(), 'state.json'), { seq: 0, jobs: {} });
}

function isRunning(id) {
  const job = readState().jobs[id];
  return job && job.status === 'running';
}

// ------------------------------------------------------------------ Shorts: online + outbox

const mediaUrl = file => `media://local/${encodeURIComponent(file)}`;

function localVideo(post) {
  const local = post.slug && post.rendered_file ? path.join(settings.repoDir, 'shorts', post.slug, post.rendered_file) : null;
  return local && fs.existsSync(local) ? local : null;
}

function statusOf(item) {
  if (item.where === 'outbox') return 'uploading';
  if (item.where === 'posted') return 'posted';
  // A result.json still in ready/ means n8n never finished with it: the upload failed for this
  // Short (copyright), was never confirmed, or went up but couldn't be moved. n8n skips these.
  if (item.result) return 'check';
  return item.post.hold ? 'held' : 'waiting';
}

async function listShorts() {
  const items = [];
  for (const id of outboxPosts()) {
    const dir = path.join(outboxDir(), id);
    const post = readJson(path.join(dir, 'post.json'));
    if (!post) continue;
    const video = localVideo(post) || path.join(dir, post.video || 'video.mp4');
    const f = failures.get(id);
    items.push({ id, where: 'outbox', post, videoUrl: fs.existsSync(video) ? mediaUrl(video) : null,
      uploadError: f ? f.error : null });
  }
  let error = null;
  if (drive && drive.signedIn) {
    try {
      for (const it of await drive.listPosts()) {
        const local = localVideo(it.post);
        items.push({ ...it, videoUrl: local ? mediaUrl(local) : it.videoUrl });
      }
    } catch (e) {
      error = e.message;
    }
  } else {
    error = settings.msClientId ? 'Not signed in to OneDrive: connect it in Settings' : 'Set up OneDrive in Settings';
  }
  return { items: items.map(it => ({ ...it, status: statusOf(it) })), error };
}

function cleanPatch(patch) {
  const next = {};
  if (typeof patch.title === 'string') next.title = patch.title.trim().slice(0, 100);
  if (typeof patch.description === 'string') next.description = patch.description.slice(0, 5000);
  if (Array.isArray(patch.tags)) next.tags = patch.tags.map(t => String(t).trim()).filter(Boolean);
  if (PRIORITIES.includes(patch.priority)) next.priority = patch.priority;
  if (typeof patch.hold === 'boolean') next.hold = patch.hold;
  return next;
}

// Not uploaded yet: edit the outbox copy (the upload then carries the edit). Otherwise edit online.
async function savePost(id, patch) {
  const clean = cleanPatch(patch);
  const local = path.join(outboxDir(), id, 'post.json');
  if (!isInside(local, outboxDir())) throw new Error('bad post id');
  if (fs.existsSync(local) && upload.active !== id) {
    const post = { ...readJson(local), ...clean, edited_at: new Date().toISOString() };
    writeJsonAtomic(local, post);
    return post;
  }
  if (!drive || !drive.signedIn) throw new Error('Connect OneDrive in Settings first');
  try {
    return await drive.updatePost(id, clean);
  } catch (e) {
    if (e.status === 404) throw new Error('This Short is no longer waiting (already posted?)');
    throw e;
  }
}

async function resolveCheck(id, action) {
  if (!drive || !drive.signedIn) throw new Error('Connect OneDrive in Settings first');
  if (action === 'retry') await drive.retryPost(id);
  else if (action === 'posted') await drive.markPosted(id);
}

// A job's priority also applies to its Shorts that are still waiting (outbox or online ready/).
async function setJobPostsPriority(jobId, priority) {
  const job = readState().jobs[jobId];
  for (const id of (job && job.posts) || []) {
    try {
      await savePost(id, { priority });
    } catch { /* posted already, or offline: leave it */ }
  }
  emitToUi('shorts-changed', {});
}

// ------------------------------------------------------------------ checks

async function checkVoiceStudio() {
  try {
    const res = await net.fetch(`${settings.voicestudioUrl.replace(/\/$/, '')}/.well-known/voicestudio-speech`,
      { signal: AbortSignal.timeout(4000) });
    return res.ok ? { ok: true, message: 'answering' } : { ok: false, message: `HTTP ${res.status}` };
  } catch (e) {
    return { ok: false, message: 'not answering. Start VoiceStudio.' };
  }
}

function checkCommand(cmd, args) {
  return new Promise(resolve => {
    let out = '';
    const p = spawn(cmd, args, { windowsHide: true, shell: process.platform === 'win32' });
    p.stdout.on('data', d => { out += d; });
    p.on('error', () => resolve({ ok: false, message: 'not found on PATH' }));
    p.on('exit', code => resolve(code === 0
      ? { ok: true, message: out.trim().split('\n')[0] } : { ok: false, message: `exit ${code}` }));
  });
}

async function checkOneDrive() {
  if (!settings.msClientId) return { ok: false, message: 'set the Microsoft app (client) ID' };
  if (!drive || !drive.signedIn) return { ok: false, message: 'not connected' };
  try {
    account = account || await drive.me();
    await drive.ensureFolders();
    return { ok: true, message: `${account.email}: ${settings.onedriveFolder}/ is ready` };
  } catch (e) {
    return { ok: false, message: e.message };
  }
}

async function runChecks() {
  const [voicestudio, claude, uv, onedrive] = await Promise.all([
    checkVoiceStudio(),
    checkCommand(process.env.CLAUDE_BIN || 'claude', ['--version']),
    checkCommand(process.env.STREAMVAULT_UV || 'uv', ['--version']),
    checkOneDrive(),
  ]);
  return {
    voicestudio, claude, uv, onedrive,
    repo: fs.existsSync(path.join(settings.repoDir, 'clipper', 'run_queue.py'))
      ? { ok: true, message: settings.repoDir } : { ok: false, message: 'clipper/run_queue.py not found' },
  };
}

// ------------------------------------------------------------------ IPC

function handle(channel, fn) {
  ipcMain.handle(channel, async (_e, ...args) => fn(...args));
}

handle('get-settings', () => settings);
handle('set-settings', next => saveSettings(next));
handle('get-state', () => ({
  state: readState(), pending: pendingAdds(), engine: engineStatus, engineLog, upload, onedrive: oneDriveStatus(),
}));
handle('run-checks', runChecks);
handle('restart-engine', () => restartEngine());

handle('onedrive-status', async () => {
  if (drive && drive.signedIn && !account) account = await drive.me().catch(() => null);
  return oneDriveStatus();
});
handle('onedrive-connect', connectOneDrive);
handle('onedrive-cancel', () => { login = null; sendOneDriveStatus(); });
handle('onedrive-disconnect', disconnectOneDrive);
handle('upload-now', () => { failures.clear(); uploadSoon(0); });

handle('add-links', ({ links, section, priority }) => {
  const urls = [...new Set(String(links).match(/https?:\/\/[^\s"'<>]+/g) || [])]
    .map(u => u.replace(/[).,;\]]+$/, ''));
  const sec = /^\d[\d:]*-\d[\d:]*$/.test(String(section || '').trim()) ? section.trim() : null;
  for (const url of urls) sendRequest({ op: 'add', kind: 'url', target: url, section: sec, priority });
  sendPending();
  return urls.length;
});
handle('add-files', ({ files, priority }) => {
  const ok = files.filter(f => VIDEO_EXTS.includes(path.extname(f).slice(1).toLowerCase()) && fs.existsSync(f));
  for (const f of ok) sendRequest({ op: 'add', kind: 'file', target: f, priority });
  sendPending();
  return ok.length;
});
handle('remove-pending', name => {
  // Only a request file name, never a path.
  if (/^[\w-]+\.json$/.test(String(name))) fs.rmSync(path.join(workerDir(), 'requests', name), { force: true });
  sendPending();
});
handle('pick-files', async () => {
  const r = await dialog.showOpenDialog(win, {
    title: 'Add videos', properties: ['openFile', 'multiSelections'],
    filters: [{ name: 'Videos', extensions: VIDEO_EXTS }],
  });
  return r.canceled ? [] : r.filePaths;
});
handle('pick-folder', async () => {
  const r = await dialog.showOpenDialog(win, { properties: ['openDirectory', 'createDirectory'] });
  return r.canceled ? null : r.filePaths[0];
});
handle('job-action', async ({ op, id, priority }) => {
  sendRequest({ op, id, priority });
  // A running job only stops when its process tree does; the engine cancels it on restart.
  if (op === 'cancel' && isRunning(id)) await restartEngine();
  if (op === 'priority' && PRIORITIES.includes(priority)) setJobPostsPriority(id, priority);
});
handle('job-log', id => {
  try {
    const text = fs.readFileSync(path.join(workerDir(), 'logs', `${id}.log`), 'utf8');
    return text.split('\n').slice(-300).join('\n');
  } catch {
    return '';
  }
});
handle('list-shorts', listShorts);
handle('save-post', ({ id, patch }) => savePost(id, patch));
handle('resolve-check', ({ id, action }) => resolveCheck(id, action));
handle('open-external', url => {
  if (/^https?:\/\//.test(url)) shell.openExternal(url);
});
handle('show-folder', async () => {
  const url = drive && drive.signedIn ? await drive.folderUrl().catch(() => null) : null;
  if (url) shell.openExternal(url);
  else shell.openPath(path.join(settings.repoDir, 'shorts'));
});


// ------------------------------------------------------------------ setup (the Setup tab)

let workspace = null;
let secrets = null;
let pythonStale = false;   // video-use was updated: `uv sync` again before the engine runs

const setupLog = step => line => emitToUi('setup-log', { step, line: String(line).slice(0, 400) });
const n8nClient = () => new N8n(settings.n8nUrl, secrets.get('n8nApiKey'));
const github = () => new GitHub(secrets.get('githubToken'), settings.githubRepo);

function freeSpace(dir) {
  try {
    let d = dir;
    while (!fs.existsSync(d) && path.dirname(d) !== d) d = path.dirname(d);
    const st = fs.statfsSync(d);
    return Math.round((st.bavail * st.bsize) / 1e9);
  } catch {
    return null;
  }
}

const CHECKS = {
  async workspace() {
    const st = workspace.status(settings.repoDir);
    const gb = freeSpace(settings.repoDir);
    return { ...st, ok: st.ok && !pythonStale, dir: settings.repoDir, freeGb: gb,
      message: `${st.message}${gb != null ? ` · ${gb} GB free` : ''}` };
  },
  tools: () => setup.checkTools({ voicestudioUrl: settings.voicestudioUrl }),
  async python() {
    const r = await setup.pythonEnv(settings.repoDir);
    return pythonStale ? { ok: false, message: 'scripts were updated: set up the environment again' } : r;
  },
  claude: () => setup.claudeStatus(),
  async voicestudio() {
    return (await setup.voicestudioAnswers(settings.voicestudioUrl))
      ? { ok: true, message: `answering at ${settings.voicestudioUrl}` }
      : { ok: false, message: `not answering at ${settings.voicestudioUrl}. Start VoiceStudio.` };
  },
  async n8n() {
    const c = n8nClient();
    if (!settings.n8nUrl || !secrets.get('n8nApiKey')) return { ok: false, message: 'add the n8n URL and API key', redirectUrl: c.redirectUrl };
    return { ...(await c.check()), redirectUrl: c.redirectUrl };
  },
  async microsoft() {
    const od = oneDriveStatus();
    const secret = !!secrets.get('msClientSecret');
    if (!settings.msClientId) return { ok: false, message: 'add the Azure app (client) ID', ...od, secret };
    if (!secret) return { ok: false, message: 'add the client secret (n8n needs it)', ...od, secret };
    if (!od.signedIn) return { ok: false, message: 'connect OneDrive', ...od, secret };
    if (!account && drive) account = await drive.me().catch(() => null);
    return { ok: true, message: `OneDrive: ${account ? account.email : 'connected'}`, ...oneDriveStatus(), secret };
  },
  async github() {
    const r = await github().check();
    let session = null;
    if (r.user) session = await github().secretInfo('YT_SESSION').catch(() => null);
    const sessionMsg = session ? `YouTube session saved ${new Date(session.updated_at).toLocaleString()}` : 'YouTube session not saved yet';
    return { ...r, ok: r.ok && !!session, session: session && session.updated_at,
      message: r.ok ? `${r.message} · ${sessionMsg}` : r.message };
  },
  async youtubeApi() {
    const set = !!(settings.googleClientId && secrets.get('googleClientSecret'));
    return { ok: true, optional: true, set, message: set ? 'Google OAuth client saved' : 'skipped: posting goes through Studio' };
  },
  async workflow() {
    const id = settings.n8nIds.workflow;
    if (!id) return { ok: false, message: 'not created yet' };
    try {
      const st = await n8nClient().status(id);
      if (!st.exists) return { ok: false, message: 'the workflow was deleted in n8n: create it again' };
      const c = n8nClient();
      return { ok: st.active, ...st, url: c.workflowUrl(id),
        credentials: Object.fromEntries(Object.entries(settings.n8nIds).filter(([k]) => k !== 'workflow')
          .map(([k, v]) => [k, { ...v, url: c.credentialUrl(v.id) }])),
        message: st.active ? 'active: posting on schedule' : 'created, not active yet' };
    } catch (e) {
      return { ok: false, message: e.message };
    }
  },
};

async function saveSetup(values) {
  const secretKeys = ['n8nApiKey', 'msClientSecret', 'githubToken', 'googleClientSecret'];
  secrets.set(Object.fromEntries(Object.entries(values).filter(([k]) => secretKeys.includes(k))));
  const plain = Object.fromEntries(Object.entries(values).filter(([k]) => !secretKeys.includes(k)));
  if (Object.keys(plain).length) saveSettings(plain);
}

async function createWorkflow(log) {
  const n = n8nClient();
  const gh = await github().check();
  if (!gh.user) throw new Error(`GitHub: ${gh.message}`);
  const ids = { ...settings.n8nIds };
  log('creating credentials in n8n…');
  ids.onedrive = await n.ensureCredential('onedrive',
    { clientId: settings.msClientId, clientSecret: secrets.get('msClientSecret') }, ids.onedrive);
  log(`${ids.onedrive.created ? 'created' : 'kept'} ${ids.onedrive.name}`);
  ids.github = await n.ensureCredential('github',
    { server: 'https://api.github.com', user: gh.user, accessToken: secrets.get('githubToken') }, ids.github);
  log(`${ids.github.created ? 'created' : 'kept'} ${ids.github.name}`);
  if (settings.googleClientId && secrets.get('googleClientSecret')) {
    ids.youtube = await n.ensureCredential('youtube',
      { clientId: settings.googleClientId, clientSecret: secrets.get('googleClientSecret') }, ids.youtube);
    log(`${ids.youtube.created ? 'created' : 'kept'} ${ids.youtube.name}`);
  } else {
    delete ids.youtube;
  }
  const body = buildWorkflow(workspace.template(settings.repoDir), ids, {
    folder: settings.onedriveFolder, repo: settings.githubRepo, branch: gh.defaultBranch,
    hours: settings.postEveryHours, timezone: settings.timezone, uploader: settings.uploader,
  });
  const wf = await n.upsertWorkflow(body, ids.workflow);
  ids.workflow = wf.id;
  saveSettings({ n8nIds: ids });
  log(`${wf.created ? 'created' : 'updated'} the workflow "${body.name}" (${wf.id})`);
  log('Next: open each OAuth credential in n8n and click "Connect", then Activate.');
}

const ACTIONS = {
  async 'workspace.choose'() {
    const r = await dialog.showOpenDialog(win, { properties: ['openDirectory', 'createDirectory'], defaultPath: settings.repoDir });
    if (!r.canceled) saveSettings({ repoDir: r.filePaths[0] });
  },
  async 'workspace.copy'(_v, log) {
    if (!workspace.bundleDir) return log('running from the repo: nothing to copy');
    await stopEngine();
    workspace.sync(settings.repoDir);
    pythonStale = true;
    log(`copied the scripts into ${settings.repoDir}`);
    startEngine();
  },
  'tools.install': ({ id }, log) => setup.installTool(id, { voicestudioUrl: settings.voicestudioUrl }, log),
  async 'python.sync'(_v, log) {
    const r = await setup.syncPython(settings.repoDir, log);
    pythonStale = false;
    await restartEngine();
    return r;
  },
  'claude.login': () => setup.claudeLogin(),
  'voicestudio.test': () => setup.testVoiceStudio(settings.voicestudioUrl),
  'voicestudio.timeout': () => setup.setVoiceStudioTimeout(),
  'save': values => saveSetup(values),
  'microsoft.connect': () => connectOneDrive(),
  'microsoft.disconnect': () => disconnectOneDrive(),
  'github.openChrome': () => setup.openChromeForYouTube(path.join(app.getPath('userData'), 'yt-session-profile')),
  async 'github.capture'(_v, log) {
    const { session, cookies } = await setup.captureYouTubeSession();
    log(`read ${cookies} YouTube/Google cookies`);
    const gh = github();
    await gh.setSecret('YT_SESSION', session);
    log('saved YT_SESSION on GitHub');
    await gh.setSecret('SECRETS_PAT', secrets.get('githubToken'));
    log('saved SECRETS_PAT (lets each upload run keep the session fresh)');
    saveSettings({ sessionSavedAt: new Date().toISOString() });
  },
  'workflow.create': (_v, log) => createWorkflow(log),
  async 'workflow.activate'(_v, log) {
    await n8nClient().activate(settings.n8nIds.workflow);
    log('activated');
  },
  'open': ({ url }) => { if (/^https?:\/\//.test(url)) shell.openExternal(url); },
};

handle('setup-check', async step => {
  try {
    return await CHECKS[step]();
  } catch (e) {
    return { ok: false, message: e.message };
  }
});
handle('setup-action', async ({ action, values }) => {
  const step = action.split('.')[0];
  const log = setupLog(step);
  try {
    const result = await ACTIONS[action](values || {}, log);
    return { ok: true, result };
  } catch (e) {
    log(`error: ${e.message}`);
    return { ok: false, error: e.message };
  }
});
handle('setup-secrets', () => secrets.masked());

// ------------------------------------------------------------------ windows

function createWindow(show = true) {
  win = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 820,
    minHeight: 560,
    show,
    title: 'StreamVault',
    icon: path.join(__dirname, 'assets', 'icon.png'),
    backgroundColor: '#0f0f14',
    autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false },
  });
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  win.on('close', e => {
    // Closing hides to the tray so Shorts keep being made and uploaded; Quit is in the tray menu.
    if (!quitting) {
      e.preventDefault();
      win.hide();
    }
  });
}

function showWindow() {
  if (!win) createWindow();
  win.show();
  win.focus();
}

function updateTrayMenu() {
  if (!tray) return;
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Open StreamVault', click: showWindow },
    { label: 'Open OneDrive folder', click: async () => {
      const url = drive && drive.signedIn ? await drive.folderUrl().catch(() => null) : null;
      if (url) shell.openExternal(url);
    } },
    { type: 'separator' },
    { label: 'Quit', click: quit },
  ]));
}

async function quit() {
  const running = Object.values(readState().jobs).some(j => j.status === 'running');
  if (running || uploading) {
    const r = await dialog.showMessageBox(win, {
      type: 'question', buttons: ['Quit anyway', 'Keep running'], defaultId: 1, cancelId: 1,
      message: running ? 'A video is being processed.' : 'A Short is uploading to OneDrive.',
      detail: running ? 'Quitting stops it. It will start again from its last finished step next time.'
        : 'Quitting stops the upload. It starts over next time.',
    });
    if (r.response !== 0) return;
  }
  quitting = true;
  await stopEngine();
  app.quit();
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', showWindow);
  app.whenReady().then(async () => {
    loadSettings();
    secrets = new Secrets(path.join(app.getPath('userData'), 'secrets.bin'), safeStorage);
    workspace = new Workspace(app.isPackaged ? path.join(process.resourcesPath, 'workspace') : null);
    // An app update brings new scripts: copy them over an existing workspace right away.
    const ws = workspace.status(settings.repoDir);
    if (ws.needsCopy && ws.installed) {
      workspace.sync(settings.repoDir);
      pythonStale = true;
    }
    await setup.refreshPath();
    drive = makeDrive();
    protocol.handle('media', req => {
      const file = decodeURIComponent(new URL(req.url).pathname.slice(1));
      // Only videos inside the StreamVault folder (shorts/, outbox/).
      if (!isInside(file, settings.repoDir)) return new Response('forbidden', { status: 403 });
      return net.fetch(pathToFileURL(file).toString(), { headers: req.headers });
    });
    tray = new Tray(nativeImage.createFromPath(path.join(__dirname, 'assets', 'tray.png')).resize({ width: 16, height: 16 }));
    tray.on('click', showWindow);
    updateTrayMenu();
    createWindow(!process.argv.includes('--hidden'));
    startEngine();
    uploadSoon(3000);
    if (drive.signedIn) drive.me().then(me => { account = me; sendOneDriveStatus(); }).catch(() => {});
  });
  app.on('before-quit', () => { quitting = true; });
  app.on('will-quit', () => { if (engine) killTree(engine); });
  app.on('window-all-closed', () => { /* stay in the tray */ });
}
