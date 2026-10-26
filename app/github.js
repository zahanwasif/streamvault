// GitHub for the Studio uploader: check the repo, and store repo secrets (YT_SESSION, SECRETS_PAT).
// Secrets are sealed with the repo's public key (libsodium sealed box), as GitHub requires.

const sodium = require('libsodium-wrappers');

const API = (process.env.STREAMVAULT_GITHUB_URL || 'https://api.github.com').replace(/\/$/, '');
const WORKFLOW = 'youtube-upload.yml';

class GitHub {
  constructor(token, repo) {
    this.token = token;
    this.repo = repo;
  }

  async api(method, path, body) {
    const res = await fetch(API + path, {
      method,
      headers: {
        Authorization: `Bearer ${this.token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    const data = text ? JSON.parse(text) : null;
    if (!res.ok) {
      const err = new Error(`GitHub ${res.status}: ${(data && data.message) || text.slice(0, 200)}`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  // { ok, message, user, defaultBranch, hasWorkflow }
  async check() {
    if (!this.token) return { ok: false, message: 'add a GitHub token' };
    if (!/^[\w.-]+\/[\w.-]+$/.test(this.repo || '')) return { ok: false, message: 'repo should look like owner/name' };
    let user, repo;
    try {
      user = await this.api('GET', '/user');
      repo = await this.api('GET', `/repos/${this.repo}`);
    } catch (e) {
      return { ok: false, message: e.status === 401 ? 'the token was rejected' : e.status === 404 ? `can't see ${this.repo} with this token` : e.message };
    }
    let hasWorkflow = true;
    try {
      await this.api('GET', `/repos/${this.repo}/contents/.github/workflows/${WORKFLOW}?ref=${encodeURIComponent(repo.default_branch)}`);
    } catch {
      hasWorkflow = false;
    }
    const base = { user: user.login, defaultBranch: repo.default_branch, hasWorkflow };
    if (!hasWorkflow) {
      return { ok: false, ...base,
        message: `${WORKFLOW} isn't on ${repo.default_branch} yet: merge the StreamVault branch so n8n can start uploads` };
    }
    return { ok: true, ...base, message: `${user.login} → ${this.repo} (${repo.default_branch})` };
  }

  async setSecret(name, value) {
    await sodium.ready;
    const { key, key_id: keyId } = await this.api('GET', `/repos/${this.repo}/actions/secrets/public-key`);
    const sealed = sodium.crypto_box_seal(sodium.from_string(value), sodium.from_base64(key, sodium.base64_variants.ORIGINAL));
    await this.api('PUT', `/repos/${this.repo}/actions/secrets/${name}`, {
      encrypted_value: sodium.to_base64(sealed, sodium.base64_variants.ORIGINAL), key_id: keyId,
    });
  }

  async secretInfo(name) {
    try {
      return await this.api('GET', `/repos/${this.repo}/actions/secrets/${name}`);
    } catch (e) {
      if (e.status === 404) return null;
      throw e;
    }
  }
}

module.exports = { GitHub };
