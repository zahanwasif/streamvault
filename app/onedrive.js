// Online OneDrive through Microsoft Graph: sign-in (device code), uploads, and the posts n8n reads.
//
// Everything lives under one folder in the drive root, StreamVault/ by default, with the same
// layout n8n's poster uses: ready/<post id>/{video.mp4, post.json}, posted/<post id>/, failures/.
// Paths, not item ids, address everything, so a post is always "<folder>/ready/<post id>".

const fs = require('fs');
const path = require('path');

const LOGIN = (process.env.STREAMVAULT_LOGIN_URL || 'https://login.microsoftonline.com/common/oauth2/v2.0').replace(/\/$/, '');
const GRAPH = (process.env.STREAMVAULT_GRAPH_URL || 'https://graph.microsoft.com/v1.0').replace(/\/$/, '');
const SCOPE = 'Files.ReadWrite offline_access User.Read';
const CHUNK = 320 * 1024 * 32; // 10 MiB; Graph wants upload chunks in multiples of 320 KiB
const POSTED_LIMIT = 60;       // posted/ grows forever; the app shows the most recent ones

class NotSignedIn extends Error {
  constructor(msg = 'Not signed in to OneDrive') { super(msg); this.code = 'NOT_SIGNED_IN'; }
}

class GraphError extends Error {
  constructor(status, body, url) {
    let msg = body;
    try { msg = JSON.parse(body).error?.message || body; } catch { /* plain text */ }
    super(`OneDrive ${status}: ${String(msg).slice(0, 300)}`);
    this.status = status;
    this.url = url;
  }
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function form(url, fields) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams(fields),
  });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, data };
}

async function pool(items, size, fn) {
  const out = new Array(items.length);
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(size, items.length) }, async () => {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i], i);
    }
  }));
  return out;
}

class OneDrive {
  // store: { load() -> token|null, save(token), clear() }
  constructor({ clientId, folder, store }) {
    this.clientId = clientId;
    this.folder = (folder || 'StreamVault').replace(/^\/+|\/+$/g, '');
    this.store = store;
    this.tok = store.load();
    this.ensured = false;
  }

  get signedIn() { return !!(this.tok && this.tok.refresh_token); }

  // ---------------------------------------------------------------- sign-in

  async startDeviceLogin() {
    if (!this.clientId) throw new Error('Set the Microsoft app (client) ID in Settings first');
    const { ok, data } = await form(`${LOGIN}/devicecode`, { client_id: this.clientId, scope: SCOPE });
    if (!ok) throw new Error(data.error_description || data.error || 'Microsoft refused the sign-in request');
    return data; // user_code, device_code, verification_uri, expires_in, interval, message
  }

  async pollDeviceLogin(dc, isCancelled = () => false) {
    let interval = Number(dc.interval || 5) * 1000;
    const deadline = Date.now() + Number(dc.expires_in || 900) * 1000;
    while (Date.now() < deadline && !isCancelled()) {
      await sleep(interval);
      const { ok, data } = await form(`${LOGIN}/token`, {
        client_id: this.clientId, device_code: dc.device_code,
        grant_type: 'urn:ietf:params:oauth:grant-type:device_code',
      });
      if (ok) {
        this.saveToken(data);
        return this.me();
      }
      if (data.error === 'authorization_pending') continue;
      if (data.error === 'slow_down') { interval += 5000; continue; }
      throw new Error(data.error_description || data.error || 'Sign-in failed');
    }
    throw new Error(isCancelled() ? 'Sign-in cancelled' : 'The sign-in code expired. Try again.');
  }

  saveToken(data) {
    this.tok = {
      access_token: data.access_token,
      refresh_token: data.refresh_token || this.tok?.refresh_token,
      expires_at: Date.now() + Number(data.expires_in || 3600) * 1000,
    };
    this.store.save(this.tok);
  }

  signOut() {
    this.tok = null;
    this.ensured = false;
    this.store.clear();
  }

  async accessToken(force = false) {
    if (!this.signedIn) throw new NotSignedIn();
    if (!force && this.tok.access_token && this.tok.expires_at - Date.now() > 5 * 60 * 1000) return this.tok.access_token;
    const { ok, data } = await form(`${LOGIN}/token`, {
      client_id: this.clientId, grant_type: 'refresh_token', refresh_token: this.tok.refresh_token, scope: SCOPE,
    });
    if (!ok) {
      if (data.error === 'invalid_grant' || data.error === 'interaction_required') {
        this.signOut();
        throw new NotSignedIn('OneDrive sign-in expired: connect OneDrive again in Settings');
      }
      throw new Error(data.error_description || data.error || 'Could not refresh the OneDrive sign-in');
    }
    this.saveToken(data); // Microsoft rotates the refresh token; keep the new one
    return this.tok.access_token;
  }

  // ---------------------------------------------------------------- requests

  // method, "/me/..." or a full URL; json body or raw body; returns parsed JSON, text, or null.
  async graph(method, url, { json, body, headers = {}, as = 'json', auth = true } = {}) {
    const full = url.startsWith('http') ? url : GRAPH + url;
    let refresh = false;
    for (let attempt = 0; ; attempt++) {
      const h = { ...headers };
      if (auth) h.Authorization = `Bearer ${await this.accessToken(refresh)}`;
      if (json !== undefined) h['Content-Type'] = 'application/json';
      let res;
      try {
        res = await fetch(full, { method, headers: h, body: json !== undefined ? JSON.stringify(json) : body });
      } catch (e) {
        if (attempt < 3) { await sleep(2000 * (attempt + 1)); continue; }
        throw new Error(`OneDrive unreachable: ${e.message}`);
      }
      if (res.status === 401 && auth && !refresh) { // expired early or revoked: refresh once
        refresh = true;
        continue;
      }
      refresh = false;
      if ((res.status === 429 || res.status >= 500) && attempt < 4) {
        const wait = Number(res.headers.get('retry-after')) * 1000 || 1500 * 2 ** attempt;
        await sleep(Math.min(wait, 60000));
        continue;
      }
      if (!res.ok) throw new GraphError(res.status, await res.text(), full);
      if (res.status === 204 || as === 'none') return null;
      if (as === 'text') return res.text();
      return res.json();
    }
  }

  p(rel = '') {
    const parts = [this.folder, ...String(rel).split('/')].filter(Boolean).map(encodeURIComponent);
    return `/me/drive/root:/${parts.join('/')}`;
  }

  async exists(rel) {
    try {
      return await this.graph('GET', `${this.p(rel)}?$select=id,name,size,webUrl,folder`);
    } catch (e) {
      if (e.status === 404) return null;
      throw e;
    }
  }

  async children(rel) {
    const items = [];
    let url = `${this.p(rel)}:/children?$top=200`;
    while (url) {
      let page;
      try {
        page = await this.graph('GET', url);
      } catch (e) {
        if (e.status === 404) return items;
        throw e;
      }
      items.push(...(page.value || []));
      url = page['@odata.nextLink'];
    }
    return items;
  }

  async me() {
    const m = await this.graph('GET', '/me?$select=displayName,mail,userPrincipalName');
    return { name: m.displayName, email: m.mail || m.userPrincipalName };
  }

  // A folder in the StreamVault folder (or, with inDriveRoot, the StreamVault folder itself).
  // conflictBehavior "fail": never replace a folder that already exists (that would empty it).
  async mkdir(name, inDriveRoot = false) {
    const parent = inDriveRoot ? '/me/drive/root' : `${this.p('')}:`;
    return this.graph('POST', `${parent}/children`, {
      json: { name, folder: {}, '@microsoft.graph.conflictBehavior': 'fail' },
    }).catch(e => { if (e.status !== 409) throw e; });
  }

  async ensureFolders() {
    if (this.ensured) return;
    if (!(await this.exists(''))) await this.mkdir(this.folder, true);
    for (const sub of ['ready', 'posted', 'failures']) {
      if (!(await this.exists(sub))) await this.mkdir(sub);
    }
    this.ensured = true;
  }

  async folderUrl() {
    const f = await this.exists('');
    return f && f.webUrl;
  }

  // ---------------------------------------------------------------- upload

  // Upload outbox/<id>/ as <folder>/ready/<id>/: video.mp4 first (resumable session), post.json
  // last, then check the size. A post that n8n already moved to posted/ is never uploaded again.
  async uploadPost(localDir, onProgress = () => {}) {
    await this.ensureFolders();
    const id = path.basename(localDir);
    const post = JSON.parse(fs.readFileSync(path.join(localDir, 'post.json'), 'utf8'));
    const videoPath = path.join(localDir, post.video || 'video.mp4');
    const size = fs.statSync(videoPath).size;

    if (await this.exists(`posted/${id}`)) return { skipped: 'already posted' };
    const remotePost = await this.exists(`ready/${id}/post.json`);
    const remoteVideo = await this.exists(`ready/${id}/${post.video || 'video.mp4'}`);
    if (remotePost && remoteVideo && remoteVideo.size === size) return { skipped: 'already uploaded' };

    // Put post.json back last: remove a stale one so n8n can't pick up half a post.
    if (remotePost) await this.graph('DELETE', `${this.p(`ready/${id}/post.json`)}`, { as: 'none' }).catch(() => {});
    await this.graph('POST', `${this.p('ready')}:/children`, {
      json: { name: id, folder: {}, '@microsoft.graph.conflictBehavior': 'fail' },
    }).catch(e => { if (e.status !== 409) throw e; });

    const session = await this.graph('POST', `${this.p(`ready/${id}/${post.video || 'video.mp4'}`)}:/createUploadSession`, {
      json: { item: { '@microsoft.graph.conflictBehavior': 'replace' } },
    });
    const fh = await fs.promises.open(videoPath, 'r');
    try {
      let offset = 0;
      while (offset < size) {
        const len = Math.min(CHUNK, size - offset);
        const buf = Buffer.alloc(len);
        await fh.read(buf, 0, len, offset);
        // The upload URL is pre-authorised: no bearer token on these.
        await this.graph('PUT', session.uploadUrl, {
          auth: false, body: buf,
          headers: { 'Content-Length': String(len), 'Content-Range': `bytes ${offset}-${offset + len - 1}/${size}` },
        });
        offset += len;
        onProgress(offset / size);
      }
    } finally {
      await fh.close();
    }

    await this.graph('PUT', `${this.p(`ready/${id}/post.json`)}:/content`, {
      body: JSON.stringify(post, null, 2), headers: { 'Content-Type': 'application/json' },
    });
    const check = await this.exists(`ready/${id}/${post.video || 'video.mp4'}`);
    if (!check || check.size !== size) throw new Error(`upload check failed: OneDrive has ${check?.size} of ${size} bytes`);
    return { uploaded: true };
  }

  // ---------------------------------------------------------------- posts

  async listPosts() {
    await this.ensureFolders();
    const [ready, posted, failures] = await Promise.all([
      this.children('ready'), this.children('posted'), this.children('failures'),
    ]);
    const shots = Object.fromEntries(failures.filter(f => f.file)
      .map(f => [f.name.replace(/\.jpg$/, ''), f['@microsoft.graph.downloadUrl']]));
    const recentPosted = posted.filter(f => f.folder)
      .sort((a, b) => String(b.lastModifiedDateTime).localeCompare(String(a.lastModifiedDateTime)))
      .slice(0, POSTED_LIMIT);
    const folders = [
      ...ready.filter(f => f.folder).map(f => ({ f, where: 'ready' })),
      ...recentPosted.map(f => ({ f, where: 'posted' })),
    ];
    const items = await pool(folders, 6, async ({ f, where }) => {
      const files = Object.fromEntries((await this.children(`${where}/${f.name}`)).filter(x => x.file).map(x => [x.name, x]));
      if (!files['post.json']) return null; // still uploading
      const get = async name => files[name]
        ? this.graph('GET', files[name]['@microsoft.graph.downloadUrl'], { auth: false }).catch(() => null) : null;
      const [post, result] = await Promise.all([get('post.json'), get('result.json')]);
      if (!post) return null;
      const video = files[post.video || 'video.mp4'];
      return {
        id: f.name, where, post, result,
        videoUrl: video ? video['@microsoft.graph.downloadUrl'] : null,
        screenshotUrl: where === 'ready' && result ? shots[f.name] || null : null,
        webUrl: f.webUrl,
      };
    });
    return items.filter(Boolean);
  }

  async readPost(where, id) {
    return this.graph('GET', `${this.p(`${where}/${id}/post.json`)}:/content`);
  }

  async writePost(where, id, post) {
    await this.graph('PUT', `${this.p(`${where}/${id}/post.json`)}:/content`, {
      body: JSON.stringify(post, null, 2), headers: { 'Content-Type': 'application/json' },
    });
  }

  async updatePost(id, patch) {
    const post = { ...(await this.readPost('ready', id)), ...patch, edited_at: new Date().toISOString() };
    await this.writePost('ready', id, post);
    return post;
  }

  async retryPost(id) {
    await this.graph('DELETE', this.p(`ready/${id}/result.json`), { as: 'none' }).catch(e => {
      if (e.status !== 404) throw e;
    });
  }

  async markPosted(id) {
    await this.ensureFolders();
    const posted = await this.exists('posted');
    await this.graph('PATCH', this.p(`ready/${id}`), { json: { parentReference: { id: posted.id } } });
  }
}

module.exports = { OneDrive, NotSignedIn, GraphError };
