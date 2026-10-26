// Secrets the setup collects (n8n API key, Azure client secret, GitHub token, Google client secret),
// kept apart from settings.json in one file encrypted with the OS keychain (DPAPI on Windows).
// Where no keychain is available (some Linux desktops) the file is readable by this user only.

const fs = require('fs');
const path = require('path');

class Secrets {
  constructor(file, safeStorage) {
    this.file = file;
    this.safe = safeStorage;
    this.data = this.load();
  }

  load() {
    try {
      const raw = fs.readFileSync(this.file);
      const text = raw.subarray(0, 4).toString() === 'TXT:' ? raw.subarray(4).toString() : this.safe.decryptString(raw);
      return JSON.parse(text);
    } catch {
      return {};
    }
  }

  save() {
    const text = JSON.stringify(this.data);
    const data = this.safe.isEncryptionAvailable() ? this.safe.encryptString(text) : Buffer.from(`TXT:${text}`);
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    fs.writeFileSync(this.file, data, { mode: 0o600 });
  }

  get(key) {
    return this.data[key] || '';
  }

  // Empty or masked values leave the stored secret alone, so the UI never has to show it.
  set(values) {
    for (const [k, v] of Object.entries(values)) {
      if (typeof v !== 'string' || !v.trim() || v.includes('•')) continue;
      this.data[k] = v.trim();
    }
    this.save();
  }

  clear(key) {
    delete this.data[key];
    this.save();
  }

  // What the UI may see: which secrets are set, as "••••last4".
  masked() {
    return Object.fromEntries(Object.entries(this.data).map(([k, v]) => [k, v ? `••••${String(v).slice(-4)}` : '']));
  }
}

module.exports = { Secrets };
