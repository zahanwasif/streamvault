// The Short style: <workspace>/style.json, edited in the Style tab.
//
// The user sets only the selection rules (clip: minScore, maxShorts, minSeconds, maxSeconds) and
// plain-language instructions. Claude turns the instructions into each clip's look (the spec's
// "style", zooms, sfx, voice hook...); the defaults live in clipper/style_defaults.json, the file
// Python uses. The engine reads style.json fresh for every video, so saving needs no restart.
// style.json, music/ and sfx/ are user data: workspace sync never touches them, and the repo ignores them.

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const MOODS = ['funny', 'hype', 'romantic', 'chill'];
const SFX_CATEGORIES = ['transition', 'impact', 'reaction', 'ui'];
const AUDIO_RE = /\.(mp3|m4a|aac|wav|ogg|opus|flac)$/i;
const USER_KEYS = ['clip', 'instructions'];

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
const pick = obj => Object.fromEntries(USER_KEYS.filter(k => k in (obj || {})).map(k => [k, obj[k]]));

// Run a workspace script; resolves { code, lines } with every output line.
function run(cmd, args, { cwd, env, log, input, shell = false }) {
  return new Promise((resolve, reject) => {
    const p = spawn(cmd, args, { cwd, env, windowsHide: true, shell });
    const lines = [];
    let out = '';
    const onData = d => {
      out += d;
      String(d).split(/\r?\n/).filter(Boolean).forEach(l => { lines.push(l); if (log) log(l); });
    };
    p.stdout.on('data', onData);
    p.stderr.on('data', onData);
    p.on('error', reject);
    p.on('exit', code => resolve({ code, lines, out }));
    if (input != null) p.stdin.end(input);
  });
}

class Style {
  constructor(repoDir) {
    this.repoDir = repoDir;
  }

  get file() { return path.join(this.repoDir, 'style.json'); }

  defaults() {
    return readJson(path.join(this.repoDir, 'clipper', 'style_defaults.json'), {});
  }

  // { style: {clip, instructions} (defaults < style.json), defaults, exists }
  get() {
    const saved = readJson(this.file, null);
    const d = this.defaults();
    const clip = { ...(d.clip || {}), ...(isObj(saved) && isObj(saved.clip) ? saved.clip : {}) };
    const instructions = isObj(saved) && typeof saved.instructions === 'string' ? saved.instructions : (d.instructions || '');
    return { style: { clip, instructions }, defaults: pick(d), exists: !!saved };
  }

  set(style) {
    if (!isObj(style)) throw new Error('style must be an object');
    // Only the user's keys; Python cleans every value again before using it.
    writeJsonAtomic(this.file, pick(style));
    return this.get();
  }

  // 1.1: Settings held minScore/maxShorts. 1.2: style.json also held the look. Keep only the user's keys.
  migrate(settings) {
    const saved = readJson(this.file, null);
    if (isObj(saved)) {
      const kept = pick(saved);
      if (Object.keys(saved).length === Object.keys(kept).length) return false;
      writeJsonAtomic(this.file, kept);
      return true;
    }
    const clip = {};
    if (Number(settings.minScore) > 0) clip.minScore = Number(settings.minScore);
    if (Number(settings.maxShorts) > 0) clip.maxShorts = Number(settings.maxShorts);
    if (!Object.keys(clip).length) return false;
    writeJsonAtomic(this.file, { clip });
    return true;
  }

  musicDir() { return path.join(this.repoDir, 'music'); }
  sfxDir() { return path.join(this.repoDir, 'sfx'); }

  // Makes music/<mood>/ and sfx/<category>/; counts what each holds.
  music() {
    const count = d => {
      try {
        return fs.readdirSync(d).filter(n => AUDIO_RE.test(n)).length;
      } catch {
        return 0;
      }
    };
    for (const m of MOODS) fs.mkdirSync(path.join(this.musicDir(), m), { recursive: true });
    for (const c of SFX_CATEGORIES) fs.mkdirSync(path.join(this.sfxDir(), c), { recursive: true });
    return {
      dir: this.musicDir(), sfxDir: this.sfxDir(),
      moods: Object.fromEntries([['any mood', count(this.musicDir())], ...MOODS.map(m => [m, count(path.join(this.musicDir(), m))])]),
      sfx: Object.fromEntries(SFX_CATEGORIES.map(c => [c, count(path.join(this.sfxDir(), c))])),
      review: count(path.join(this.musicDir(), '_review')),
    };
  }

  // clipper/music_import.py on each link: download, skip tracks with talking, sort by mood.
  async importMusic(links, mood, { uv, env, log }) {
    const urls = [...new Set(String(links).match(/https?:\/\/[^\s"'<>]+/g) || [])];
    if (!urls.length) throw new Error('No links found');
    const args = ['run', '--project', 'video-use', 'python', 'clipper/music_import.py', ...urls];
    if (MOODS.includes(mood)) args.push('--mood', mood);
    const r = await run(uv, args, { cwd: this.repoDir, env, log });
    const lines = r.lines.filter(l => /^(ADDED|REVIEW|FAILED)/.test(l));
    return { ok: r.code === 0, lines: lines.length ? lines : r.lines.slice(-4), library: this.music() };
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
          id: `${slug}/${name}`, slug, name, title: meta.title || name, spec, category: meta.category,
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

  // What is said in the clip, "[812.4] words…", so Claude can place hooks, zooms and effects.
  transcriptText(spec) {
    const key = path.basename(spec.source || '', path.extname(spec.source || ''));
    const tr = readJson(path.join(spec.edit_dir || '', 'transcripts', `${key}.json`), {});
    const ranges = [...(spec.ranges || []), ...(spec.cold_open ? [spec.cold_open] : [])];
    const words = (tr.words || []).filter(w => w.type === 'word' && ranges.some(r => w.start >= r.start && w.start < r.end));
    const lines = [];
    for (let i = 0; i < words.length; i += 10) {
      lines.push(`[${words[i].start.toFixed(1)}] ${words.slice(i, i + 10).map(w => w.text.trim()).join(' ')}`);
    }
    return lines.slice(0, 80).join('\n');
  }

  styleKeysReference() {
    const skill = fs.readFileSync(path.join(this.repoDir, '.claude', 'skills', 'shorts-clipper', 'SKILL.md'), 'utf8');
    const m = skill.match(/## Style keys\n([\s\S]*?)(?=\n## )/);
    return m ? m[1].trim() : '';
  }

  sfxNames() {
    const lib = readJson(path.join(this.sfxDir(), 'library.json'), {});
    const files = (lib.sounds || []).map(s => s.file);
    for (const c of SFX_CATEGORIES) {
      try {
        for (const n of fs.readdirSync(path.join(this.sfxDir(), c))) if (AUDIO_RE.test(n)) files.push(`${c}/${n}`);
      } catch { /* none */ }
    }
    return [...new Set(files)].sort();
  }

  // Claude applies the instructions to one clip: returns the look JSON it chose.
  async planLook(spec, meta, style, { claude, env, log }) {
    const prompt = [
      'You are previewing the look of one YouTube Short for the StreamVault app. Apply the user\'s',
      'instructions to this clip and answer with ONLY a JSON object, no prose and no code fence.',
      '',
      `User's instructions: """${(style.instructions || '').trim() || '(none: use the defaults)'}"""`,
      '',
      `The clip: category ${meta.category || 'unknown'}, ranges (source seconds) ${JSON.stringify(spec.ranges)},`,
      `current cold_open ${JSON.stringify(spec.cold_open || null)}, current hook_text ${JSON.stringify(spec.hook_text || null)}.`,
      'What is said, with source-second timestamps:',
      this.transcriptText(spec) || '(no transcript)',
      '',
      'Return any of these keys, exactly in these shapes (times are source seconds); only include what',
      'the instructions or the defaults call for:',
      '  "hook_text": "<= 8 words of curiosity, not the punchline",',
      '  "cold_open": {"start": s, "end": s} (the payoff, inside the ranges) or null for none,',
      '  "music_mood": "funny" | "hype" | "romantic" | "chill",',
      '  "emphasis": ["word", ...] (caption words to colour),',
      '  "zooms": [{"at": s, "dur": 0.6, "scale": 1.15}, ...] (punch-ins on reactions and punch words),',
      '  "sfx": [{"at": s, "sound": "<a file below, or category/tag like impact/boom>"}, ...],',
      '  "voice_hook": "<line to speak over the cold open>" or true (speak hook_text),',
      '  "style": {...} (only keys that differ from the defaults; see the reference).',
      `Sound effects available: ${this.sfxNames().join(', ') || '(none yet: use category/tag names)'}`,
      '',
      'Style keys reference:',
      this.styleKeysReference(),
      '',
      `Defaults: ${JSON.stringify(Object.fromEntries(Object.entries(this.defaults()).filter(([k]) => !USER_KEYS.includes(k))))}`,
    ].join('\n');
    log('rendering: Claude is choosing the look…');
    const r = await run(claude, ['-p', '--output-format', 'json', '--max-turns', '2'],
      { cwd: this.repoDir, env, input: prompt, shell: process.platform === 'win32' });
    let text = '';
    try {
      text = JSON.parse(r.out.trim().split('\n').filter(l => l.startsWith('{')).pop()).result || '';
    } catch {
      throw new Error(`Claude didn't answer (exit ${r.code}): ${r.lines.slice(-2).join(' ')}`);
    }
    const start = text.indexOf('{');
    const end = text.lastIndexOf('}');
    try {
      return JSON.parse(text.slice(start, end + 1));
    } catch {
      throw new Error(`Claude's answer wasn't JSON: ${text.slice(0, 200)}`);
    }
  }

  // Claude applies `style` (unsaved instructions included) to a past Short, then a draft render.
  async preview(id, style, { uv, claude, env, log }) {
    const item = this.shorts(() => null).find(s => s.id === id);
    if (!item) throw new Error('That Short is gone');
    if (!item.ok) throw new Error(`Can't preview this one: ${item.why}`);
    const dir = path.join(this.repoDir, '.worker', 'preview');
    fs.mkdirSync(dir, { recursive: true });
    const spec = readJson(item.spec, {});
    const meta = spec.meta || {};
    const plan = await this.planLook(spec, meta, style, { claude, env, log });
    for (const k of ['hook_text', 'music_mood', 'emphasis', 'zooms', 'sfx', 'voice_hook', 'style']) {
      if (k in plan) spec[k] = plan[k];
    }
    // Shorts made before face tracking used a fixed centre crop; following the face is never worse
    // (a subject who stays put keeps the crop still).
    if (spec.layout && spec.layout.type === 'center') spec.layout = { ...spec.layout, type: 'track' };
    if ('cold_open' in plan) {
      if (plan.cold_open) spec.cold_open = plan.cold_open;
      else delete spec.cold_open;
    }
    const stem = item.name.replace(/\.mp4$/, '');
    spec.output = path.join(dir, `${stem}_preview.mp4`);
    delete spec.meta;   // a preview never touches the real Short's metadata
    const specFile = path.join(dir, `${stem}.json`);
    const styleFile = path.join(dir, 'style.json');
    writeJsonAtomic(specFile, spec);
    writeJsonAtomic(styleFile, pick(style));
    const r = await run(uv, ['run', '--project', 'video-use', 'python', 'clipper/render_short.py', specFile,
      '--draft', '--jobs', '1', '--style', styleFile], { cwd: this.repoDir, env, log });
    const report = r.lines.filter(l => /^\[(OK |BAD|ERR)\]/.test(l)).pop();
    if (!fs.existsSync(spec.output) || !report || report.startsWith('[ERR]')) {
      throw new Error(`preview render failed (exit ${r.code}): ${(report || r.lines.slice(-2).join(' ')).slice(0, 300)}`);
    }
    return { file: spec.output, report, plan };
  }
}

module.exports = { Style, MOODS };
