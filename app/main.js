// StreamVault desktop app: main process.
//
// Runs the Python engine (clipper/run_queue.py --watch) as a child process and talks to it through
// files in <repo>/.worker/: the app writes requests/*.json, the engine writes state.json and prints
// `EVENT {json}` lines. Shorts are read and edited straight from the OneDrive folder
// (ready/<post id>/post.json), which is all the n8n poster ever sees.

const { app, BrowserWindow, Tray, Menu, ipcMain, dialog, shell, protocol, net, nativeImage } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const readline = require('readline');
const { pathToFileURL } = require('url');

const VIDEO_EXTS = ['mp4', 'mov', 'mkv', 'avi', 'm4v', 'webm'];
const PRIORITIES = ['high', 'normal', 'low'];
const LOCKED_EXIT = 3; // run_queue.py: another engine holds the lock

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
  const od = process.env.STREAMVAULT_ONEDRIVE
    || (process.env.OneDrive ? path.join(process.env.OneDrive, 'StreamVault') : '');
  return {
    repoDir: app.isPackaged ? 'C:\\StreamVault' : path.resolve(__dirname, '..'),
    outputDir: od,
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

function saveSettings(next) {
  const before = JSON.stringify(settings);
  settings = { ...settings, ...next };
  writeJsonAtomic(settingsFile(), settings);
  app.setLoginItemSettings({ openAtLogin: !!settings.startAtLogin, args: ['--hidden'] });
  if (JSON.stringify(settings) !== before) restartEngine();
  return settings;
}

// ------------------------------------------------------------------ files

const workerDir = () => path.join(settings.repoDir, '.worker');

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

function isInside(child, parent) {
  if (!parent) return false;
  const rel = path.relative(path.resolve(parent), path.resolve(child));
  return rel && !rel.startsWith('..') && !path.isAbsolute(rel);
}

// ------------------------------------------------------------------ engine

function emitToUi(channel, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

function setEngineStatus(state, message = '') {
  engineStatus = { state, message };
  emitToUi('engine-status', engineStatus);
  if (tray) tray.setToolTip(`StreamVault: ${state}${message ? ` (${message})` : ''}`);
}

function startEngine() {
  clearTimeout(restartTimer);
  if (engine) return;
  if (!settings.outputDir) {
    setEngineStatus('stopped', 'Set the output folder in Settings');
    return;
  }
  if (!fs.existsSync(path.join(settings.repoDir, 'clipper', 'run_queue.py'))) {
    setEngineStatus('stopped', `StreamVault folder not found: ${settings.repoDir}`);
    return;
  }
  const args = ['run', '--project', 'video-use', 'python', 'clipper/run_queue.py', '--watch', '--parent-pipe',
    '--onedrive', settings.outputDir];
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
    if (ev.type === 'job' && ev.job.status === 'done') updateTrayMenu();
    emitToUi('engine-event', ev);
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

// ------------------------------------------------------------------ shorts in OneDrive

function listShorts() {
  const out = [];
  for (const where of ['ready', 'posted']) {
    const dir = path.join(settings.outputDir || '', where);
    let names = [];
    try {
      names = fs.readdirSync(dir, { withFileTypes: true }).filter(d => d.isDirectory()).map(d => d.name);
    } catch {
      continue;
    }
    for (const name of names) {
      const folder = path.join(dir, name);
      const post = readJson(path.join(folder, 'post.json'));
      if (!post) continue;
      const result = readJson(path.join(folder, 'result.json'));
      const local = post.slug && post.rendered_file
        ? path.join(settings.repoDir, 'shorts', post.slug, post.rendered_file) : null;
      const video = local && fs.existsSync(local) ? local : path.join(folder, post.video || 'video.mp4');
      out.push({
        id: name,
        where,
        status: where === 'posted' ? 'posted' : post.hold ? 'held' : 'waiting',
        post,
        result,
        videoUrl: fs.existsSync(video) ? `media://local/${encodeURIComponent(video)}` : null,
      });
    }
  }
  return out;
}

function savePost(id, patch) {
  const file = path.join(settings.outputDir, 'ready', id, 'post.json');
  if (!isInside(file, path.join(settings.outputDir, 'ready'))) throw new Error('bad post id');
  const post = readJson(file);
  if (!post) throw new Error('This Short is no longer waiting (already posted?)');
  const next = { ...post };
  if (typeof patch.title === 'string') next.title = patch.title.trim().slice(0, 100);
  if (typeof patch.description === 'string') next.description = patch.description.slice(0, 5000);
  if (Array.isArray(patch.tags)) next.tags = patch.tags.map(t => String(t).trim()).filter(Boolean);
  if (PRIORITIES.includes(patch.priority)) next.priority = patch.priority;
  if (typeof patch.hold === 'boolean') next.hold = patch.hold;
  next.edited_at = new Date().toISOString();
  writeJsonAtomic(file, next);
  return next;
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

async function runChecks() {
  const outputOk = !!settings.outputDir && fs.existsSync(settings.outputDir);
  const inOneDrive = !!process.env.OneDrive && isInside(settings.outputDir, process.env.OneDrive);
  return {
    voicestudio: await checkVoiceStudio(),
    claude: await checkCommand(process.env.CLAUDE_BIN || 'claude', ['--version']),
    uv: await checkCommand(process.env.STREAMVAULT_UV || 'uv', ['--version']),
    repo: fs.existsSync(path.join(settings.repoDir, 'clipper', 'run_queue.py'))
      ? { ok: true, message: settings.repoDir } : { ok: false, message: 'clipper/run_queue.py not found' },
    output: !outputOk ? { ok: false, message: settings.outputDir ? 'folder does not exist yet' : 'not set' }
      : inOneDrive || !process.env.OneDrive ? { ok: true, message: settings.outputDir }
        : { ok: false, message: 'outside OneDrive: n8n in the cloud cannot read this folder' },
  };
}

// ------------------------------------------------------------------ IPC

function handle(channel, fn) {
  ipcMain.handle(channel, async (_e, ...args) => fn(...args));
}

handle('get-settings', () => settings);
handle('set-settings', next => saveSettings(next));
handle('get-state', () => ({ state: readState(), engine: engineStatus, engineLog }));
handle('run-checks', runChecks);
handle('restart-engine', () => restartEngine());

handle('add-links', ({ links, section, priority }) => {
  const urls = [...new Set(String(links).match(/https?:\/\/[^\s"'<>]+/g) || [])]
    .map(u => u.replace(/[).,;\]]+$/, ''));
  const sec = /^\d[\d:]*-\d[\d:]*$/.test(String(section || '').trim()) ? section.trim() : null;
  for (const url of urls) sendRequest({ op: 'add', kind: 'url', target: url, section: sec, priority });
  return urls.length;
});
handle('add-files', ({ files, priority }) => {
  const ok = files.filter(f => VIDEO_EXTS.includes(path.extname(f).slice(1).toLowerCase()) && fs.existsSync(f));
  for (const f of ok) sendRequest({ op: 'add', kind: 'file', target: f, priority });
  return ok.length;
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
handle('open-external', url => {
  if (/^https?:\/\//.test(url)) shell.openExternal(url);
});
handle('show-folder', which => {
  const p = which === 'output' ? settings.outputDir : path.join(settings.repoDir, 'shorts');
  if (p) shell.openPath(p);
});

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
    // Closing hides to the tray so Shorts keep being made; Quit is in the tray menu.
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
    { label: 'Open output folder', click: () => settings.outputDir && shell.openPath(settings.outputDir) },
    { type: 'separator' },
    { label: 'Quit', click: quit },
  ]));
}

async function quit() {
  const running = Object.values(readState().jobs).some(j => j.status === 'running');
  if (running) {
    const r = await dialog.showMessageBox(win, {
      type: 'question', buttons: ['Quit anyway', 'Keep running'], defaultId: 1, cancelId: 1,
      message: 'A video is being processed.',
      detail: 'Quitting stops it. It will start again from its last finished step next time.',
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
  app.whenReady().then(() => {
    loadSettings();
    protocol.handle('media', req => {
      const file = decodeURIComponent(new URL(req.url).pathname.slice(1));
      // Only videos under the StreamVault and output folders.
      if (!isInside(file, settings.repoDir) && !isInside(file, settings.outputDir)) {
        return new Response('forbidden', { status: 403 });
      }
      return net.fetch(pathToFileURL(file).toString(), { headers: req.headers });
    });
    tray = new Tray(nativeImage.createFromPath(path.join(__dirname, 'assets', 'tray.png')).resize({ width: 16, height: 16 }));
    tray.on('click', showWindow);
    updateTrayMenu();
    createWindow(!process.argv.includes('--hidden'));
    startEngine();
  });
  app.on('before-quit', () => { quitting = true; });
  app.on('will-quit', () => { if (engine) killTree(engine); });
  app.on('window-all-closed', () => { /* stay in the tray */ });
}
