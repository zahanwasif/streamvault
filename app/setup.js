// PC setup the app does itself: tools, PATH, the Python environment, the video-use skill link,
// Claude sign-in, VoiceStudio, and capturing the YouTube session from Chrome.
//
// Every tool is a row in TOOLS (check + install command), so tests can swap the table
// (STREAMVAULT_TOOLS_JSON). Installs only ever run when the user clicks Install.

const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const zlib = require('zlib');

const WIN = process.platform === 'win32';
const winget = id => ({ cmd: 'winget', args: ['install', '--id', id, '-e', '--silent',
  '--accept-source-agreements', '--accept-package-agreements'] });
const powershell = script => ({ cmd: 'powershell.exe', args: ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script] });

const TOOLS = [
  { id: 'uv', name: 'uv', why: 'runs the Python scripts', check: { cmd: 'uv', args: ['--version'] },
    install: winget('astral-sh.uv') },
  { id: 'ffmpeg', name: 'ffmpeg', why: 'cuts and renders the Shorts', check: { cmd: 'ffmpeg', args: ['-version'] },
    install: winget('Gyan.FFmpeg') },
  { id: 'node', name: 'Node.js', why: 'yt-dlp needs it to read YouTube', check: { cmd: 'node', args: ['--version'] },
    install: winget('OpenJS.NodeJS.LTS') },
  { id: 'ytdlp', name: 'yt-dlp', why: 'downloads YouTube and Twitch videos', needs: ['uv'],
    check: { cmd: 'yt-dlp', args: ['--version'] }, install: { cmd: 'uv', args: ['tool', 'install', '--force', 'yt-dlp[default]'] } },
  { id: 'claude', name: 'Claude Code', why: 'picks the best moments and renders the Shorts',
    check: { cmd: 'claude', args: ['--version'] }, install: powershell('irm https://claude.ai/install.ps1 | iex') },
  { id: 'chrome', name: 'Google Chrome', why: 'to sign in to YouTube for the Studio uploader',
    check: { chrome: true }, install: winget('Google.Chrome') },
  { id: 'voicestudio', name: 'VoiceStudio', why: 'transcribes on your GPU (WhisperX)',
    check: { voicestudio: true }, install: powershell('irm https://voicestudio.sh/install | iex') },
];

function tools() {
  if (process.env.STREAMVAULT_TOOLS_JSON) return JSON.parse(fs.readFileSync(process.env.STREAMVAULT_TOOLS_JSON, 'utf8'));
  return TOOLS;
}

// ------------------------------------------------------------------ PATH

const EXTRA_DIRS = () => [
  path.join(os.homedir(), '.local', 'bin'),                                     // uv tools, Claude Code
  ...(WIN ? [path.join(process.env.LOCALAPPDATA || '', 'Microsoft', 'WinGet', 'Links'),
    path.join(process.env.ProgramFiles || 'C:\\Program Files', 'nodejs')] : []),
];

function regPath(key) {
  return new Promise(resolve => {
    const p = spawn('reg', ['query', key, '/v', 'Path'], { windowsHide: true });
    let out = '';
    p.stdout.on('data', d => { out += d; });
    p.on('error', () => resolve(''));
    p.on('exit', () => {
      const m = out.match(/Path\s+REG_(?:EXPAND_)?SZ\s+(.*)/i);
      resolve(m ? m[1].trim().replace(/%([^%]+)%/g, (_, v) => process.env[v] || `%${v}%`) : '');
    });
  });
}

// Programs installed since the app started are on the registry PATH, not on ours. Rebuild ours
// (machine + user + well-known install folders) so they work without restarting the app.
async function refreshPath() {
  let parts = (process.env.PATH || '').split(path.delimiter);
  if (WIN) {
    const machine = await regPath('HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment');
    const user = await regPath('HKCU\\Environment');
    parts = [...machine.split(';'), ...user.split(';'), ...parts];
  }
  parts = [...parts, ...EXTRA_DIRS()].filter(Boolean);
  const seen = new Set();
  process.env.PATH = parts.filter(p => {
    const k = WIN ? p.toLowerCase() : p;
    return !seen.has(k) && seen.add(k);
  }).join(path.delimiter);
  return process.env.PATH;
}

// ------------------------------------------------------------------ running commands

// Run a command, streaming output lines to onLine. Resolves { code, out }.
function run(cmd, args, { cwd, onLine = () => {}, env, timeout = 0 } = {}) {
  return new Promise(resolve => {
    let out = '';
    let p;
    try {
      // .cmd/.bat shims (npm installs) need a shell on Windows; real .exe and powershell don't.
      const shell = WIN && !/powershell|winget|reg|setx/i.test(cmd);
      p = spawn(cmd, args.map(a => (shell && /\s/.test(a) ? `"${a}"` : a)),
        { cwd, env: { ...process.env, ...env }, windowsHide: true, shell });
    } catch (e) {
      return resolve({ code: -1, out: e.message });
    }
    const timer = timeout ? setTimeout(() => p.kill(), timeout) : null;
    const feed = d => {
      const s = d.toString();
      out += s;
      s.split(/\r?\n/).filter(l => l.trim()).forEach(onLine);
    };
    p.stdout.on('data', feed);
    p.stderr.on('data', feed);
    p.on('error', e => { clearTimeout(timer); resolve({ code: -1, out: e.code === 'ENOENT' ? 'not found' : e.message }); });
    p.on('exit', code => { clearTimeout(timer); resolve({ code, out }); });
  });
}

// ------------------------------------------------------------------ checks

function chromePath() {
  const c = [
    path.join(process.env.ProgramFiles || 'C:\\Program Files', 'Google', 'Chrome', 'Application', 'chrome.exe'),
    path.join(process.env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)', 'Google', 'Chrome', 'Application', 'chrome.exe'),
    path.join(process.env.LOCALAPPDATA || '', 'Google', 'Chrome', 'Application', 'chrome.exe'),
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium',
    process.env.STREAMVAULT_CHROME || '',
  ];
  return c.find(p => p && fs.existsSync(p)) || null;
}

async function voicestudioAnswers(url) {
  try {
    const res = await fetch(`${url.replace(/\/$/, '')}/.well-known/voicestudio-speech`, { signal: AbortSignal.timeout(4000) });
    return res.ok;
  } catch {
    return false;
  }
}

async function checkTool(t, ctx) {
  if (t.check.chrome) {
    const p = chromePath();
    return p ? { ok: true, message: p } : { ok: false, message: 'not installed' };
  }
  if (t.check.voicestudio) {
    return (await voicestudioAnswers(ctx.voicestudioUrl))
      ? { ok: true, message: `answering at ${ctx.voicestudioUrl}` }
      : { ok: false, message: `not answering at ${ctx.voicestudioUrl} (installed but not started?)` };
  }
  const r = await run(t.check.cmd, t.check.args, { timeout: 20000 });
  return r.code === 0 ? { ok: true, message: r.out.trim().split('\n')[0].slice(0, 120) }
    : { ok: false, message: r.out.trim() === 'not found' || /not recognized|not found/i.test(r.out) ? 'not installed' : r.out.trim().slice(0, 160) };
}

async function checkTools(ctx) {
  await refreshPath();
  const out = {};
  for (const t of tools()) out[t.id] = { name: t.name, why: t.why, ...(await checkTool(t, ctx)) };
  return out;
}

async function installTool(id, ctx, onLine) {
  const t = tools().find(x => x.id === id);
  if (!t) throw new Error(`unknown tool ${id}`);
  await refreshPath();
  onLine(`$ ${t.install.cmd} ${t.install.args.join(' ')}`);
  const r = await run(t.install.cmd, t.install.args, { onLine });
  await refreshPath();
  onLine(r.code === 0 ? 'finished' : `exit code ${r.code}`);
  return checkTool(t, ctx);
}

// ------------------------------------------------------------------ Python environment + skill

async function pythonEnv(workspace) {
  // No quotes in the code: on Windows this goes through cmd, which mangles nested quotes.
  const r = await run('uv', ['run', '--project', 'video-use', 'python', '-c', 'import numpy, requests; print(6*7)'],
    { cwd: workspace, timeout: 60000 });
  if (!(r.code === 0 && /42/.test(r.out))) return { ok: false, message: 'not set up yet' };
  return skillLinked(workspace) ? { ok: true, message: 'video-use environment ready' }
    : { ok: false, message: 'packages installed, but the video-use skill isn\'t linked for Claude Code yet' };
}

async function syncPython(workspace, onLine) {
  onLine('$ uv sync (video-use)');
  const r = await run('uv', ['sync'], { cwd: path.join(workspace, 'video-use'), onLine });
  if (r.code !== 0) throw new Error(`uv sync failed (exit ${r.code})`);
  // Face tracking (clipper/track.py). Not in video-use's own dependencies, so it goes in after the
  // sync, which is exact and would remove it; `uv run` leaves it alone.
  onLine('$ uv pip install opencv-python-headless');
  const cv = await run('uv', ['pip', 'install', '--python', path.join('.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'),
    'opencv-python-headless'], { cwd: path.join(workspace, 'video-use'), onLine });
  if (cv.code !== 0) onLine('could not install OpenCV: Shorts render with a fixed crop instead of following faces');
  // The packages are in place either way; a link failure shows on the step, not as a failed sync.
  try {
    onLine(linkSkill(workspace));
  } catch (e) {
    onLine(`could not link the video-use skill: ${e.message}`);
  }
  return pythonEnv(workspace);
}

// Claude Code finds the video-use skill in ~/.claude/skills/video-use; a junction keeps its
// helpers/ next to SKILL.md (PC_SETUP.md §4).
const skillLink = () => path.join(os.homedir(), '.claude', 'skills', 'video-use');

function samePath(a, b) {
  try {
    const x = fs.realpathSync.native(a);
    const y = fs.realpathSync.native(b);
    return WIN ? x.toLowerCase() === y.toLowerCase() : x === y;
  } catch {
    return false;
  }
}

function skillLinked(workspace) {
  return samePath(skillLink(), path.join(workspace, 'video-use'));
}

// Returns a line for the log. A junction made by hand (PC_SETUP.md) to the same folder is kept;
// one pointing elsewhere is replaced; a real folder there is renamed aside, never deleted.
function linkSkill(workspace) {
  const link = skillLink();
  const target = path.join(workspace, 'video-use');
  if (skillLinked(workspace)) return `the video-use skill is already linked (${link})`;
  fs.mkdirSync(path.dirname(link), { recursive: true });
  let st = null;
  try {
    st = fs.lstatSync(link);
  } catch { /* not there yet */ }
  let note = '';
  if (st && st.isSymbolicLink()) {
    // A junction or symlink: removing it leaves its target alone. unlink fails on a Windows
    // directory junction, rmdir removes it.
    try {
      fs.unlinkSync(link);
    } catch {
      fs.rmdirSync(link);
    }
    note = ' (replaced an old link)';
  } else if (st) {
    // Outside skills/, so Claude Code doesn't load it as a second video-use skill.
    const aside = path.join(os.homedir(), '.claude', `video-use-skill.old-${Date.now()}`);
    fs.renameSync(link, aside);
    note = ` (moved the folder that was there to ${aside})`;
  }
  fs.symlinkSync(target, link, WIN ? 'junction' : 'dir');
  return `linked the video-use skill for Claude Code${note}`;
}

// ------------------------------------------------------------------ Claude sign-in

async function claudeStatus() {
  const r = await run('claude', ['auth', 'status', '--json'], { timeout: 20000 });
  try {
    const s = JSON.parse(r.out.slice(r.out.indexOf('{')));
    return s.loggedIn ? { ok: true, message: `signed in (${s.authMethod || 'Claude account'})` }
      : { ok: false, message: 'not signed in' };
  } catch {
    return { ok: false, message: r.code === -1 ? 'Claude Code is not installed yet' : 'not signed in' };
  }
}

// `claude auth login` is interactive, so it gets its own console window.
function claudeLogin() {
  if (WIN) {
    spawn('cmd.exe', ['/c', 'start "Claude sign-in" cmd /k claude auth login'],
      { windowsVerbatimArguments: true, detached: true, stdio: 'ignore' }).unref();
  } else {
    spawn('x-terminal-emulator', ['-e', 'claude auth login'], { detached: true, stdio: 'ignore' })
      .on('error', () => spawn('claude', ['auth', 'login'], { detached: true, stdio: 'ignore' }).unref())
      .unref();
  }
}

// ------------------------------------------------------------------ VoiceStudio

function toneWav(seconds = 2, rate = 16000) {
  const n = seconds * rate;
  const buf = Buffer.alloc(44 + n * 2);
  buf.write('RIFF', 0); buf.writeUInt32LE(36 + n * 2, 4); buf.write('WAVE', 8);
  buf.write('fmt ', 12); buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(rate, 24); buf.writeUInt32LE(rate * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
  buf.write('data', 36); buf.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) buf.writeInt16LE(Math.round(8000 * Math.sin(2 * Math.PI * 440 * i / rate)), 44 + i * 2);
  return buf;
}

// Send 2 s of tone through the transcription endpoint the engine uses.
async function testVoiceStudio(url) {
  const form = new FormData();
  form.append('file', new Blob([toneWav()], { type: 'audio/wav' }), 'tone.wav');
  form.append('model', 'whisper-1');
  form.append('response_format', 'verbose_json');
  form.append('timestamp_granularities[]', 'word');
  const t0 = Date.now();
  try {
    const res = await fetch(`${url.replace(/\/$/, '')}/v1/audio/transcriptions`, { method: 'POST', body: form,
      signal: AbortSignal.timeout(180000) });
    const text = await res.text();
    if (!res.ok) return { ok: false, message: `VoiceStudio returned ${res.status}: ${text.slice(0, 200)}` };
    JSON.parse(text);
    return { ok: true, message: `transcription works (${((Date.now() - t0) / 1000).toFixed(1)} s)` };
  } catch (e) {
    return { ok: false, message: `no answer: ${e.message}. Is a model loaded?` };
  }
}

async function setVoiceStudioTimeout() {
  process.env.OMNIVOICE_ASR_TRANSCRIBE_TIMEOUT_S = '3600';
  if (!WIN) return { ok: true, message: 'set for this session' };
  const r = await run('setx', ['OMNIVOICE_ASR_TRANSCRIBE_TIMEOUT_S', '3600']);
  return r.code === 0 ? { ok: true, message: 'set to 3600 s. Restart VoiceStudio to apply it.' }
    : { ok: false, message: r.out.trim() };
}

// ------------------------------------------------------------------ YouTube session from Chrome

const CDP_PORT = 9333;
let chromeProc = null;

// Chrome, normally started (its own profile), on YouTube Studio. The user signs in by hand.
function openChromeForYouTube(profileDir) {
  const exe = chromePath();
  if (!exe) throw new Error('Google Chrome is not installed (see Tools)');
  fs.mkdirSync(profileDir, { recursive: true });
  chromeProc = spawn(exe, [`--user-data-dir=${profileDir}`, `--remote-debugging-port=${CDP_PORT}`, '--no-first-run',
    '--no-default-browser-check', ...(process.env.STREAMVAULT_CHROME_ARGS || '').split(' ').filter(Boolean),
    process.env.STREAMVAULT_STUDIO_URL || 'https://studio.youtube.com'], { detached: true, stdio: 'ignore' });
  chromeProc.unref();
}

function cdp(wsUrl, method, params = {}) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const timer = setTimeout(() => { ws.close(); reject(new Error('Chrome did not answer')); }, 10000);
    ws.onopen = () => ws.send(JSON.stringify({ id: 1, method, params }));
    ws.onmessage = e => {
      const msg = JSON.parse(e.data);
      if (msg.id !== 1) return;
      clearTimeout(timer);
      ws.close();
      msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
    };
    ws.onerror = () => { clearTimeout(timer); reject(new Error('could not talk to Chrome')); };
  });
}

const SIGNED_IN = ['SAPISID', '__Secure-3PSID', 'SID'];
const SECRET_LIMIT = 48 * 1024;

// Read the youtube.com/google.com cookies from that Chrome and pack them the way
// uploader/studio_upload.py loads them: gzip(JSON list of Playwright cookies), base64.
async function captureYouTubeSession({ closeChrome = true } = {}) {
  let version;
  try {
    version = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`, { signal: AbortSignal.timeout(3000) })).json();
  } catch {
    throw new Error('The Chrome window for YouTube is not open. Click "Open Chrome" first.');
  }
  const { cookies } = await cdp(version.webSocketDebuggerUrl, 'Storage.getCookies');
  const keep = cookies.filter(c => /(^|\.)(youtube|google)\.com$/.test(c.domain))
    .map(c => ({
      name: c.name, value: c.value, domain: c.domain, path: c.path,
      expires: c.session || c.expires < 0 ? -1 : c.expires,
      httpOnly: !!c.httpOnly, secure: !!c.secure,
      sameSite: ['Strict', 'Lax', 'None'].includes(c.sameSite) ? c.sameSite : 'Lax',
    }));
  const yt = new Set(keep.filter(c => c.domain.endsWith('youtube.com')).map(c => c.name));
  if (!SIGNED_IN.some(n => yt.has(n))) {
    throw new Error('Not signed in yet: sign in to YouTube Studio in that Chrome window (the channel dashboard should show), then try again.');
  }
  const session = zlib.gzipSync(Buffer.from(JSON.stringify(keep))).toString('base64');
  if (session.length > SECRET_LIMIT) throw new Error(`the session is ${session.length} bytes, over GitHub's 48 KB secret limit`);
  if (closeChrome) await cdp(version.webSocketDebuggerUrl, 'Browser.close').catch(() => {});
  return { session, cookies: keep.length };
}

module.exports = {
  TOOLS, tools, refreshPath, run, checkTools, installTool, chromePath,
  pythonEnv, syncPython, linkSkill, skillLinked, claudeStatus, claudeLogin,
  voicestudioAnswers, testVoiceStudio, setVoiceStudioTimeout, toneWav,
  openChromeForYouTube, captureYouTubeSession,
};
