// Copy the StreamVault scripts the app runs into app/build/workspace/, for the installer to carry.
//
// The installed app copies this into the user's workspace folder (default C:\StreamVault) on
// first run and after each update; see app/workspace.js. video-use gets the Windows patch here,
// so the PC never needs git.
//
//   node scripts/stage-workspace.js
const { execFileSync } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const REPO = path.resolve(__dirname, '..', '..');
const OUT = path.resolve(__dirname, '..', 'build', 'workspace');
const SKIP = new Set(['.venv', '__pycache__', 'tests', 'out', '.git', 'node_modules', '.pytest_cache']);

// [source in the repo, destination in the workspace]
const ITEMS = [
  ['clipper', 'clipper'],
  ['uploader', 'uploader'],
  ['.claude/skills/shorts-clipper', '.claude/skills/shorts-clipper'],
  ['video-use', 'video-use'],
  ['n8n/post-shorts.json', 'n8n/post-shorts.json'],
];

function copy(src, dest) {
  const st = fs.statSync(src);
  if (st.isDirectory()) {
    fs.mkdirSync(dest, { recursive: true });
    for (const name of fs.readdirSync(src)) {
      if (SKIP.has(name) || name.endsWith('.egg-info') || name.endsWith('.pyc')) continue;
      copy(path.join(src, name), path.join(dest, name));
    }
  } else {
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.copyFileSync(src, dest);
  }
}

function hashDir(dir) {
  const h = crypto.createHash('sha256');
  const walk = d => {
    for (const name of fs.readdirSync(d).sort()) {
      const p = path.join(d, name);
      if (fs.statSync(p).isDirectory()) walk(p);
      else h.update(path.relative(dir, p).replace(/\\/g, '/')).update(fs.readFileSync(p));
    }
  };
  walk(dir);
  return h.digest('hex').slice(0, 16);
}

function patchVideoUse() {
  const cwd = path.join(OUT, 'video-use');
  const patch = path.join(REPO, 'clipper', 'video-use-windows.patch');
  // The staged copy sits inside the repo's working tree; the ceiling stops git from finding the
  // repo, so `git apply` patches these files as a plain patch tool would.
  const env = { ...process.env, GIT_CEILING_DIRECTORIES: path.dirname(cwd) };
  const git = args => execFileSync('git', args, { cwd, env, stdio: 'pipe' });
  try {
    git(['apply', '--reverse', '--check', patch]);
    console.log('video-use: Windows patch already applied');
    return;
  } catch { /* not applied yet */ }
  git(['apply', patch]);
  console.log('video-use: Windows patch applied');
}

if (!fs.existsSync(path.join(REPO, 'video-use', 'helpers', 'render.py'))) {
  console.error('video-use is missing: run `git submodule update --init` in the repo first');
  process.exit(1);
}
fs.rmSync(OUT, { recursive: true, force: true });
for (const [src, dest] of ITEMS) copy(path.join(REPO, src), path.join(OUT, dest));
patchVideoUse();

const pkg = require('../package.json');
const version = { app: pkg.version, hash: hashDir(OUT), staged: new Date().toISOString() };
fs.writeFileSync(path.join(OUT, 'workspace-version.json'), JSON.stringify(version, null, 2));
console.log(`staged workspace ${version.hash} → ${OUT}`);
