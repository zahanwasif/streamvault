// The workspace: the folder holding the scripts the engine runs plus all media and working data.
//
// The installer carries a copy of the scripts (resources/workspace, made by
// scripts/stage-workspace.js). Whenever that copy's version differs from the one recorded in the
// workspace, the code folders are replaced. Media, state and the user's own files (sources/, shorts/,
// inbox/, outbox/, .worker/, music/, fonts/, style.json) are never touched. Running unpackaged (npm start in the repo), the repo is the workspace
// and nothing is copied.

const fs = require('fs');
const path = require('path');

const CODE = ['clipper', 'uploader', path.join('.claude', 'skills', 'shorts-clipper'), 'n8n'];
const DATA = ['sources', 'shorts', 'inbox', 'outbox', '.worker', 'music', 'fonts'];
const STAMP = '.streamvault-version';

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
}

function copyTree(src, dest, skip = new Set()) {
  fs.mkdirSync(dest, { recursive: true });
  for (const name of fs.readdirSync(src)) {
    if (skip.has(name)) continue;
    const s = path.join(src, name);
    const d = path.join(dest, name);
    if (fs.statSync(s).isDirectory()) copyTree(s, d);
    else fs.copyFileSync(s, d);
  }
}

class Workspace {
  // bundleDir: resources/workspace in an installed app, null when running from the repo.
  constructor(bundleDir) {
    this.bundleDir = bundleDir && fs.existsSync(path.join(bundleDir, 'workspace-version.json')) ? bundleDir : null;
  }

  bundled() {
    return this.bundleDir ? readJson(path.join(this.bundleDir, 'workspace-version.json')) : null;
  }

  installed(dir) {
    return readJson(path.join(dir, STAMP));
  }

  // { ok, needsCopy, bundled, installed, message }
  status(dir) {
    if (!dir) return { ok: false, needsCopy: false, message: 'choose a folder' };
    const hasScripts = fs.existsSync(path.join(dir, 'clipper', 'run_queue.py'));
    if (!this.bundleDir) {
      return hasScripts ? { ok: true, needsCopy: false, message: `${dir} (running from the repo)` }
        : { ok: false, needsCopy: false, message: 'clipper/run_queue.py not found here' };
    }
    const b = this.bundled();
    const i = this.installed(dir);
    const needsCopy = !hasScripts || !i || i.hash !== b.hash;
    return {
      ok: !needsCopy, needsCopy, bundled: b, installed: i,
      message: needsCopy ? (i ? `scripts are from an older version (${i.app}); update them` : 'scripts not copied yet')
        : `scripts ${b.app} in ${dir}`,
    };
  }

  // Copy the bundled scripts into dir. Returns true if video-use changed (needs `uv sync`).
  sync(dir) {
    if (!this.bundleDir) return false;
    fs.mkdirSync(dir, { recursive: true });
    for (const d of DATA) fs.mkdirSync(path.join(dir, d), { recursive: true });
    for (const rel of CODE) {
      const src = path.join(this.bundleDir, rel);
      if (!fs.existsSync(src)) continue;
      fs.rmSync(path.join(dir, rel), { recursive: true, force: true });
      copyTree(src, path.join(dir, rel));
    }
    // video-use: replace the code but keep its .venv (rebuilt by uv sync only when needed).
    const vu = path.join(dir, 'video-use');
    if (fs.existsSync(vu)) {
      for (const name of fs.readdirSync(vu)) {
        if (name !== '.venv') fs.rmSync(path.join(vu, name), { recursive: true, force: true });
      }
    }
    copyTree(path.join(this.bundleDir, 'video-use'), vu, new Set(['.venv']));
    const b = this.bundled();
    fs.writeFileSync(path.join(dir, STAMP), JSON.stringify({ ...b, copied: new Date().toISOString() }, null, 2));
    return true;
  }

  // The n8n workflow template the app sends to n8n.
  template(dir) {
    const bundled = this.bundleDir && path.join(this.bundleDir, 'n8n', 'post-shorts.json');
    const file = bundled && fs.existsSync(bundled) ? bundled : path.join(dir, 'n8n', 'post-shorts.json');
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  }
}

module.exports = { Workspace, DATA };
