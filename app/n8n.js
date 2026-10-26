// The n8n side, through n8n's public API (X-N8N-API-KEY): create the credentials the poster
// workflow uses, build the workflow from the bundled template (n8n/post-shorts.json), and
// create/update/activate it.
//
// OAuth credentials (OneDrive, YouTube) are created with the client ID and secret only: their
// "Connect" step is a browser sign-in that only n8n's own UI can run, so the app links there.

const crypto = require('crypto');

const TEMPLATE_REPO = 'zahanwasif/streamvault';  // what the template's Dispatch upload URL names
const API_ONLY_NODES = ['Start YouTube upload', 'Download video', 'Upload video'];
const CRED_TYPES = {
  onedrive: 'microsoftOneDriveOAuth2Api',
  github: 'githubApi',
  youtube: 'youTubeOAuth2Api',
};
const CRED_NAMES = { onedrive: 'StreamVault OneDrive', github: 'StreamVault GitHub', youtube: 'StreamVault YouTube' };

const fingerprint = data => crypto.createHash('sha256').update(JSON.stringify(data)).digest('hex').slice(0, 16);

class N8n {
  constructor(baseUrl, apiKey) {
    this.base = String(baseUrl || '').replace(/\/+$/, '');
    this.key = apiKey;
  }

  get redirectUrl() { return `${this.base}/rest/oauth2-credential/callback`; }
  credentialUrl(id) { return `${this.base}/home/credentials/${id}`; }
  workflowUrl(id) { return `${this.base}/workflow/${id}`; }

  async api(method, path, body) {
    if (!this.base || !this.key) throw new Error('set the n8n URL and API key first');
    let res;
    try {
      res = await fetch(`${this.base}/api/v1${path}`, {
        method,
        headers: { 'X-N8N-API-KEY': this.key, Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json' } : {}) },
        body: body ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(30000),
      });
    } catch (e) {
      throw new Error(`can't reach n8n at ${this.base}: ${e.message}`);
    }
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { /* html error page */ }
    if (!res.ok) {
      const err = new Error(`n8n ${res.status}: ${(data && data.message) || text.slice(0, 200)}`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  async check() {
    try {
      await this.api('GET', '/workflows?limit=1');
      return { ok: true, message: `connected to ${this.base}` };
    } catch (e) {
      return { ok: false, message: e.status === 401 ? 'the API key was rejected' : e.message };
    }
  }

  // ---------------------------------------------------------------- credentials

  // Fill a credential's data from the type's schema: our values, then schema defaults for
  // anything else it requires (n8n rejects unknown or missing properties).
  async credentialData(type, values) {
    let schema = null;
    try {
      schema = await this.api('GET', `/credentials/schema/${type}`);
    } catch { /* older n8n: send what we have */ }
    if (!schema || !schema.properties) return values;
    const data = {};
    for (const [k, v] of Object.entries(values)) if (k in schema.properties) data[k] = v;
    for (const k of schema.required || []) {
      if (data[k] === undefined && schema.properties[k] && schema.properties[k].default !== undefined) {
        data[k] = schema.properties[k].default;
      }
    }
    return data;
  }

  async credentialExists(id) {
    try {
      await this.api('GET', `/credentials/${id}`);
      return true;
    } catch (e) {
      if (e.status === 404) return false;
      return true; // this n8n can't read credentials back: trust the stored id
    }
  }

  // Create the credential, or keep the stored one if its values haven't changed. An OAuth
  // credential is never patched: that would drop the sign-in done in n8n. New values = new credential.
  async ensureCredential(kind, values, stored) {
    const type = CRED_TYPES[kind];
    const fp = fingerprint(values);
    if (stored && stored.id && stored.fp === fp && await this.credentialExists(stored.id)) {
      return { ...stored, created: false };
    }
    const data = await this.credentialData(type, values);
    const cred = await this.api('POST', '/credentials', { name: CRED_NAMES[kind], type, data });
    if (stored && stored.id && stored.id !== cred.id) {
      await this.api('DELETE', `/credentials/${stored.id}`).catch(() => {});
    }
    return { id: cred.id, name: cred.name, type, fp, created: true };
  }

  // ---------------------------------------------------------------- workflow

  async workflowExists(id) {
    try {
      return await this.api('GET', `/workflows/${id}`);
    } catch (e) {
      if (e.status === 404) return null;
      throw e;
    }
  }

  async upsertWorkflow(body, storedId) {
    if (storedId && await this.workflowExists(storedId)) {
      const wf = await this.api('PUT', `/workflows/${storedId}`, body);
      return { id: wf.id, created: false };
    }
    const wf = await this.api('POST', '/workflows', body);
    return { id: wf.id, created: true };
  }

  async activate(id) {
    try {
      return await this.api('POST', `/workflows/${id}/activate`);
    } catch (e) {
      if (e.status === 404 || e.status === 405) return this.api('POST', `/workflows/${id}/publish`);
      throw e;
    }
  }

  async status(id) {
    const wf = await this.workflowExists(id);
    if (!wf) return { exists: false };
    let executions = [];
    try {
      const r = await this.api('GET', `/executions?workflowId=${id}&limit=5`);
      executions = (r.data || []).map(x => ({ id: x.id, status: x.status || (x.finished ? 'success' : 'error'),
        startedAt: x.startedAt, stoppedAt: x.stoppedAt }));
    } catch { /* executions API off */ }
    return { exists: true, active: !!(wf.active || wf.activeVersionId), name: wf.name, updatedAt: wf.updatedAt, executions };
  }
}

// Turn the bundled template into this user's workflow.
//   creds: { onedrive: {id,name}, github: {id,name}, youtube?: {id,name} }
//   opts:  { folder, repo, branch, hours, timezone, uploader }
function buildWorkflow(template, creds, opts) {
  const wf = JSON.parse(JSON.stringify(template));
  const withApi = !!creds.youtube;
  const uploader = withApi ? opts.uploader || 'browser' : 'browser';
  const folder = (opts.folder || 'StreamVault').split('/').filter(Boolean).map(encodeURIComponent).join('/');
  const typeToCred = {
    [CRED_TYPES.onedrive]: creds.onedrive, [CRED_TYPES.github]: creds.github, [CRED_TYPES.youtube]: creds.youtube,
  };
  const hours = Math.min(24, Math.max(1, Math.round(Number(opts.hours) || 4)));
  const cron = hours === 24 ? '0 12 * * *' : `0 */${hours} * * *`;

  wf.nodes = wf.nodes.filter(n => withApi || !API_ONLY_NODES.includes(n.name)).map(n => {
    const p = n.parameters;
    let s = JSON.stringify(p)
      .split('root:/StreamVault').join(`root:/${folder}`)
      .split(`repos/${TEMPLATE_REPO}/`).join(`repos/${opts.repo || TEMPLATE_REPO}/`)
      .replace("ref: 'main'", `ref: '${opts.branch || 'main'}'`);
    const params = JSON.parse(s);
    if (n.type === 'n8n-nodes-base.scheduleTrigger') params.rule = { interval: [{ field: 'cronExpression', expression: cron }] };
    if (n.type === 'n8n-nodes-base.code' && params.jsCode.includes('const UPLOADER =')) {
      params.jsCode = params.jsCode.replace(/const UPLOADER = '[a-z]+';/, `const UPLOADER = '${uploader}';`);
    }
    const node = { ...n, parameters: params };
    if (params.nodeCredentialType) {
      const c = typeToCred[params.nodeCredentialType];
      if (c) node.credentials = { [params.nodeCredentialType]: { id: c.id, name: c.name } };
    }
    if (n.type === 'n8n-nodes-base.scheduleTrigger') node.name = hours === 24 ? 'Every day' : `Every ${hours} hours`;
    return node;
  });

  // Connections: follow the schedule's rename, and drop links into removed nodes.
  const scheduleOld = template.nodes.find(n => n.type === 'n8n-nodes-base.scheduleTrigger').name;
  const scheduleNew = wf.nodes.find(n => n.type === 'n8n-nodes-base.scheduleTrigger').name;
  const kept = new Set(wf.nodes.map(n => n.name));
  const conns = {};
  for (const [from, c] of Object.entries(wf.connections)) {
    const name = from === scheduleOld ? scheduleNew : from;
    if (!kept.has(name)) continue;
    conns[name] = { main: c.main.map(out => out.filter(t => kept.has(t.node))) };
  }
  for (const n of wf.nodes) {
    if (n.type === 'n8n-nodes-base.stickyNote') {
      n.parameters.content = n.parameters.content.replace('Runs every 4 hours', hours === 24 ? 'Runs once a day' : `Runs every ${hours} hours`);
    }
  }
  return {
    name: wf.name,
    nodes: wf.nodes,
    connections: conns,
    settings: { executionOrder: 'v1', timezone: opts.timezone || wf.settings.timezone, saveManualExecutions: true },
  };
}

module.exports = { N8n, buildWorkflow, CRED_TYPES, API_ONLY_NODES };
