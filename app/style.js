// The Short style: <workspace>/style.json, edited in the Style tab.
//
// The engine reads it fresh for every video and every render (clipper/style.py), so saving needs no
// engine restart. Defaults come from clipper/style_defaults.json, the same file Python uses. style.json
// and music/ are user data: workspace sync never touches them, and the repo ignores them.

const { spawn, execFile } = require('child_process');
const fs = require('fs');
const path = require('path');

const MOODS = ['funny', 'hype', 'romantic', 'chill'];

function readJson(file, fallback = null) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return fallback;
  }
}

function writeJsonAtomic(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf8');
  fs.renameSync(tmp, file);
}

const isObj = v => v && typeof v === 'object' && !Array.isArray(v);

function merge(base, over) {
  const out = { ...base };
  for (const [k, v] of Object.entries(over || {})) out[k] = isObj(v) && isObj(base[k]) ? merge(base[k], v) : v;
  return out;
}

class Style {
  constructor(repoDir) {
    this.repoDir = repoDir;
  }

  get file() { return path.join(this.repoDir, 'style.json'); }

  defaults() {
    return readJson(path.join(this.repoDir, 'clipper', 'style_defaults.json'), {});
  }

  // { style: defaults < style.json, defaults, exists }
  get() {
    const saved = readJson(this.file, null);
    return { style: merge(this.defaults(), isObj(saved) ? saved : {}), defaults: this.defaults(), exists: !!saved };
  }

  set(style) {
    if (!isObj(style)) throw new Error('style must be an object');
    // Only the known sections; Python cleans every value again before using it.
    const keep = Object.fromEntries(Object.entries(style).filter(([k]) => k in this.defaults()));
    writeJsonAtomic(this.file, keep);
    return this.get();
  }

  // Settings once held minScore/maxShorts (0 = the skill's default); they now live in style.json.
  migrate(settings) {
    if (fs.existsSync(this.file)) return false;
    const clip = {};
    if (Number(settings.minScore) > 0) clip.minScore = Number(settings.minScore);
    if (Number(settings.maxShorts) > 0) clip.maxShorts = Number(settings.maxShorts);
    if (!Object.keys(clip).length) return false;
    writeJsonAtomic(this.file, { clip });
    return true;
  }

  musicDir() {
    const folder = this.get().style.music?.folder || 'music';
    return path.isAbsolute(folder) ? folder : path.join(this.repoDir, folder);
  }

  // Makes music/<mood>/ and reports how many tracks each holds.
  music() {
    const dir = this.musicDir();
    const count = d => {
      try {
        return fs.readdirSync(d).filter(n => /\.(mp3|m4a|aac|wav|ogg|opus|flac)$/i.test(n)).length;
      } catch {
        return 0;
      }
    };
    for (const m of MOODS) fs.mkdirSync(path.join(dir, m), { recursive: true });
    return { dir, moods: Object.fromEntries([['(any)', count(dir)], ...MOODS.map(m => [m, count(path.join(dir, m))])]) };
  }

  // Past Shorts that can be re-rendered as a preview: the clip spec and its source video must still exist.
  shorts(mediaUrl) {
    const root = path.join(this.repoDir, 'shorts');
    const out = [];
    let slugs = [];
    try {
      slugs = fs.readdirSync(root, { withFileTypes: true }).filter(d => d.isDirectory()).map(d => d.name);
    } catch {
      return [];
    }
    for (const slug of slugs) {
      const specs = path.join(this.repoDir, 'sources', slug, 'edit', 'clips');
      let names = [];
      try { names = fs.readdirSync(path.join(root, slug)).filter(n => /^\d\d_.*\.mp4$/.test(n)).sort(); } catch { continue; }
      for (const name of names) {
        const meta = readJson(path.join(root, slug, name.replace(/\.mp4$/, '.json')), {});
        const spec = this.findSpec(meta.spec, specs, name);
        const source = spec && readJson(spec, {}).source;
        out.push({
          id: `${slug}/${name}`, slug, name, title: meta.title || name, spec,
          ok: !!(spec && source && fs.existsSync(source)),
          why: !spec ? 'clip spec not found' : !(source && fs.existsSync(source)) ? 'source video was deleted' : '',
          videoUrl: mediaUrl(path.join(root, slug, name)),
        });
      }
    }
    return out.sort((a, b) => b.id.localeCompare(a.id));
  }

  findSpec(recorded, specsDir, outName) {
    if (recorded && fs.existsSync(recorded)) return recorded;
    try {
      for (const n of fs.readdirSync(specsDir).filter(x => x.endsWith('.json'))) {
        const p = path.join(specsDir, n);
        const s = readJson(p, {});
        if (s.output && path.basename(s.output) === outName) return p;
      }
    } catch { /* no specs folder */ }
    return null;
  }

  // Draft re-render of a past Short with `style` (unsaved edits included). Resolves to the mp4 path.
  preview(id, style, { uv, env, log }) {
    const item = this.shorts(() => null).find(s => s.id === id);
    if (!item) throw new Error('That Short is gone');
    if (!item.ok) throw new Error(`Can't preview this one: ${item.why}`);
    const dir = path.join(this.repoDir, '.worker', 'preview');
    fs.mkdirSync(dir, { recursive: true });
    const spec = readJson(item.spec, {});
    const stem = item.name.replace(/\.mp4$/, '');
    spec.output = path.join(dir, `${stem}_preview.mp4`);
    delete spec.meta;   // a preview never touches the real Short's metadata
    const specFile = path.join(dir, `${stem}.json`);
    const styleFile = path.join(dir, 'style.json');
    writeJsonAtomic(specFile, spec);
    writeJsonAtomic(styleFile, style);
    return new Promise((resolve, reject) => {
      const p = spawn(uv, ['run', '--project', 'video-use', 'python', 'clipper/render_short.py', specFile,
        '--draft', '--jobs', '1', '--style', styleFile], { cwd: this.repoDir, env, windowsHide: true });
      let tail = '';
      const onData = d => {
        tail = (tail + d).slice(-2000);
        String(d).split(/\r?\n/).filter(Boolean).forEach(l => log(l));
      };
      p.stdout.on('data', onData);
      p.stderr.on('data', onData);
      p.on('error', reject);
      p.on('exit', code => {
        if (fs.existsSync(spec.output) && /\[(OK |BAD)\]/.test(tail)) resolve({ file: spec.output, report: tail.trim().split('\n').pop() });
        else reject(new Error(`preview render failed (exit ${code}): ${tail.trim().split('\n').slice(-2).join(' ')}`));
      });
    });
  }
}

// Installed font families on Windows, from the registry (machine and per-user fonts).
function listFonts() {
  if (process.platform !== 'win32') return Promise.resolve([]);
  const keys = ['HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Fonts',
    'HKCU\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Fonts'];
  return Promise.all(keys.map(k => new Promise(resolve => {
    execFile('reg', ['query', k], { windowsHide: true, maxBuffer: 4 << 20 }, (err, out) => resolve(err ? '' : out));
  }))).then(outs => {
    const names = new Set();
    for (const line of outs.join('\n').split(/\r?\n/)) {
      const m = line.match(/^\s{4}(.+?)\s+REG_SZ\s/);
      if (!m) continue;
      // "Arial Black (TrueType)", "Segoe UI Bold Italic (TrueType)", "Cambria & Cambria Math (TrueType)"
      for (const part of m[1].replace(/\s*\((TrueType|OpenType|All res)\)\s*$/i, '').split(' & ')) {
        const fam = part.replace(/\s+(Regular|Bold|Italic|Oblique|Light|Semibold|SemiBold|Medium|Thin|ExtraLight|ExtraBold|Condensed|Semilight|SemiLight|Demibold|Heavy)\b.*$/i, '').trim();
        if (fam && !/^@/.test(fam)) names.add(fam);
      }
    }
    return [...names].sort((a, b) => a.localeCompare(b));
  });
}

module.exports = { Style, listFonts, MOODS };
