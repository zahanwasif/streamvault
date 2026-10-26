"""Build n8n/post-shorts.json, the importable n8n poster workflow.

Edit the workflow here rather than in the JSON, then run:  python n8n/build_workflows.py
(Changes made in the n8n editor can instead be exported back over post-shorts.json.)
"""
import json
import sys
import uuid
from pathlib import Path

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
GRAPH = "https://graph.microsoft.com/v1.0/me/drive"
BASE = f"{GRAPH}/root:/StreamVault"
OD = {"authentication": "predefinedCredentialType", "nodeCredentialType": "microsoftOneDriveOAuth2Api"}
YT = {"authentication": "predefinedCredentialType", "nodeCredentialType": "youTubeOAuth2Api"}
GH = {"authentication": "predefinedCredentialType", "nodeCredentialType": "githubApi"}
GITHUB_REPO = "zahanwasif/streamvault"      # holds .github/workflows/youtube-upload.yml
GITHUB_BRANCH = "main"                      # GitHub only dispatches workflows on the default branch


def nid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "streamvault/" + name))


def node(name, typ, version, x, y, params, **extra):
    return {"parameters": params, "id": nid(name), "name": name, "type": typ,
            "typeVersion": version, "position": [x, y], **extra}


def http(name, x, y, method, url, auth=None, headers=None, body=None, response=None, timeout=None, **extra):
    p = {"method": method, "url": url, **(auth or {})}
    if headers:
        p["sendHeaders"] = True
        p["headerParameters"] = {"parameters": [{"name": k, "value": v} for k, v in headers.items()]}
    if body:
        p["sendBody"] = True
        p.update(body)
    opts = {}
    if response:
        opts["response"] = {"response": response}
    if timeout:
        opts["timeout"] = timeout
    p["options"] = opts
    return node(name, "n8n-nodes-base.httpRequest", 4.2, x, y, p, **extra)


def code(name, x, y, js):
    return node(name, "n8n-nodes-base.code", 2, x, y, {"jsCode": js.strip() + "\n"})


def note(name, x, y, w, h, text):
    return node(name, "n8n-nodes-base.stickyNote", 1, x, y, {"content": text, "width": w, "height": h})


def link(conns, a, b, out=0):
    outs = conns.setdefault(a, {"main": []})["main"]
    while len(outs) <= out:
        outs.append([])
    outs[out].append({"node": b, "type": "main", "index": 0})


def chain(names, conns=None):
    conns = {} if conns is None else conns
    for a, b in zip(names, names[1:]):
        link(conns, a, b)
    return conns


def equals(left, right):
    """An IF/Switch condition (filter v2): left == right, as strings."""
    return {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
            "conditions": [{"id": nid(f"cond/{left}/{right}"), "leftValue": left, "rightValue": right,
                            "operator": {"type": "string", "operation": "equals"}}],
            "combinator": "and"}


# ------------------------------------------------------------------ post workflow

PICK = r"""
// Every folder waiting in ready/. Which one posts is decided in Build upload, from post.json.
const kids = $input.first().json.value ?? [];
return kids
  .filter(k => k.folder && !/^[._]/.test(k.name))
  .slice(0, 200)
  .map(k => ({ json: { folderId: k.id, folderName: k.name } }));
"""

CHOOSE = r"""
// Keep folders that have post.json (the PC writes it last) and video.mp4. A result.json means the
// Short was already uploaded but the move to posted/ failed: never upload it twice.
const out = [];
$input.all().forEach((item, i) => {
  const files = {};
  for (const f of item.json.value ?? []) if (f.file) files[f.name] = f;
  if (files['result.json'] || !files['post.json'] || !files['video.mp4']) return;
  out.push({ json: {
    ...$('Pick candidates').itemMatching(i).json,
    postUrl: files['post.json']['@microsoft.graph.downloadUrl'],
    videoUrl: files['video.mp4']['@microsoft.graph.downloadUrl'],
    videoBytes: files['video.mp4'].size,
  } });
});
return out;
"""

BUILD = r"""
// Settings
// 'browser': upload through YouTube Studio on GitHub Actions (until Google's API audit is approved).
// 'api': the YouTube Data API (public uploads need the audit).
const UPLOADER = 'browser';
const PRIVACY = 'public';      // api only: 'private' or 'unlisted' to post without going live
const CATEGORY_ID = '24';      // Entertainment
const LANGUAGE = 'en';
const PRIORITY_RANK = { high: 0, normal: 1, low: 2 };

// Rank what's waiting: priority (set in the StreamVault app), then score, then oldest.
// Skip Shorts on hold, and ones whose video is still syncing (smaller than the PC recorded).
const candidates = $input.all()
  .map((item, i) => ({ pick: $('Choose post').itemMatching(i).json, post: item.json }))
  .filter(({ pick, post }) => !post.hold && !(post.video_bytes && post.video_bytes !== pick.videoBytes))
  .sort((a, b) =>
    (PRIORITY_RANK[a.post.priority] ?? 1) - (PRIORITY_RANK[b.post.priority] ?? 1)
    || (b.post.score ?? 0) - (a.post.score ?? 0)
    || String(a.post.exported_at ?? a.pick.folderName).localeCompare(String(b.post.exported_at ?? b.pick.folderName)));
if (!candidates.length) return [];
const { pick, post } = candidates[0];

// YouTube rejects < and > in titles and descriptions.
const clean = (s, n) => {
  s = String(s ?? '').replace(/</g, '‹').replace(/>/g, '›').trim();
  return s.length <= n ? s : s.slice(0, n - 1).trimEnd() + '…';
};
const bytes = s => new TextEncoder().encode(s).length;
let description = clean(post.description, 5000);
while (bytes(description) > 5000) description = description.slice(0, -50);
const tags = [];
for (const t of post.tags ?? []) {
  const v = String(t).replace(/^#/, '').trim();
  if (v && !tags.includes(v) && [...tags, v].join(',').length <= 480) tags.push(v);
}
const title = clean(post.title, 100);
const lang = post.language ?? LANGUAGE;
// What the Studio uploader needs, as base64 JSON (a GitHub workflow input).
const studioPost = JSON.stringify({ id: pick.folderName, title, description, tags });
const postB64 = typeof Buffer !== 'undefined'
  ? Buffer.from(studioPost, 'utf8').toString('base64')
  : btoa(unescape(encodeURIComponent(studioPost)));
return [{ json: {
  ...pick,
  uploader: UPLOADER,
  postB64,
  title,
  priority: post.priority ?? 'normal',
  score: post.score ?? null,
  waiting: candidates.length,
  request: {
    snippet: { title, description, tags, categoryId: String(post.category_id ?? CATEGORY_ID),
               defaultLanguage: lang, defaultAudioLanguage: lang },
    status: { privacyStatus: PRIVACY, selfDeclaredMadeForKids: false, containsSyntheticMedia: false },
  },
} }];
"""


CHECK = r"""
// What the Studio run reported. No body = the Wait node timed out (the run never called back).
const r = $input.first().json.body;
const pick = $('Build upload').first().json;
if (!r) {
  return [{ json: { route: 'unconfirmed', status: 'unconfirmed',
    error: 'No report from the GitHub upload run within the time limit. Check YouTube Studio.' } }];
}
if (r.ok) {
  return [{ json: { route: 'ok', video_id: r.video_id, url: r.url, privacy: r.privacy, uploader: 'studio' } }];
}
// kind 'post' (e.g. a copyright claim) is about this Short: mark it so it isn't retried.
// Anything else (signed out, upload limit, Studio changed) leaves it in ready/ for the next run.
const out = { json: { route: r.kind === 'post' ? 'post_failed' : 'error', status: 'failed',
                      kind: r.kind, error: r.error } };
if (r.screenshot_b64) {
  out.binary = { data: { data: r.screenshot_b64, mimeType: 'image/jpeg',
                         fileName: `${pick.folderName}.jpg`, fileExtension: 'jpg' } };
}
return [out];
"""

post_nodes = [
    note("About", -220, -380, 560, 560,
         "## StreamVault: post the next Short\n\n"
         "Runs every 4 hours. Looks at every folder in **OneDrive/StreamVault/ready/**, skips Shorts "
         "on hold, and picks the highest priority (set in the StreamVault app), then the best score, "
         "then the oldest. Uploads its video.mp4 to YouTube with the title, description and tags "
         "from post.json, writes result.json next to it and moves the folder to **posted/**.\n\n"
         "**Uploader** (top of *Build upload*): `'browser'` dispatches the GitHub workflow that uploads "
         "through YouTube Studio and waits up to 45 min for its report; `'api'` uses the YouTube Data API "
         "(needs Google's audit for public videos).\n\n"
         "Nothing waiting = nothing happens. A run that fails (signed out, upload limit, Studio changed) "
         "leaves the Short in ready/ and shows red in Executions; a Short that fails itself (copyright) or "
         "never gets a report gets a result.json and is skipped from then on. Screenshots of failures go "
         "to **failures/**.\n\n"
         "**Change how often:** open *Every 4 hours* and edit the cron (minute hour …).\n"
         "**Privacy, category, language:** top of *Build upload*."),
    node("Every 4 hours", "n8n-nodes-base.scheduleTrigger", 1.2, 0, 200,
         {"rule": {"interval": [{"field": "cronExpression", "expression": "0 */4 * * *"}]}}),
    node("Test now", "n8n-nodes-base.manualTrigger", 1, 0, 400, {}),
    http("List ready", 240, 300, "GET", f"{BASE}/ready:/children?$top=200", OD),
    code("Pick candidates", 460, 300, PICK),
    http("List files", 680, 300, "GET", f"={GRAPH}/items/{{{{ $json.folderId }}}}/children", OD),
    code("Choose post", 900, 300, CHOOSE),
    http("Get post.json", 1120, 300, "GET", "={{ $json.postUrl }}", response={"responseFormat": "json"}),
    code("Build upload", 1340, 300, BUILD),
    http("Start YouTube upload", 1780, 520, "POST",
         "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status", YT,
         headers={"X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": "={{ $json.videoBytes }}"},
         body={"specifyBody": "json", "jsonBody": "={{ JSON.stringify($json.request) }}"},
         response={"fullResponse": True}),
    http("Download video", 2000, 520, "GET", "={{ $('Build upload').first().json.videoUrl }}",
         response={"responseFormat": "file"}, timeout=600000),
    http("Upload video", 2220, 520, "PUT", "={{ $('Start YouTube upload').first().json.headers.location }}", YT,
         headers={"Content-Type": "video/mp4"},
         body={"contentType": "binaryData", "inputDataFieldName": "data"}, timeout=1800000),
    http("Save result.json", 2880, -160, "PUT",
         f"={GRAPH}/items/{{{{ $('Build upload').first().json.folderId }}}}:/result.json:/content", OD,
         body={"specifyBody": "json", "jsonBody":
               "={{ JSON.stringify({ status: 'posted', video_id: $json.video_id ?? $json.id, "
               "url: $json.url ?? ('https://youtube.com/shorts/' + $json.id), "
               "title: $('Build upload').first().json.title, privacy: $json.privacy ?? $json.status?.privacyStatus, "
               "uploader: $json.uploader ?? 'api', posted_at: $now.toISO() }, null, 2) }}"}),
    http("Find posted folder", 3100, -160, "GET", f"{BASE}/posted", OD),
    http("Move to posted", 3320, -160, "PATCH",
         f"={GRAPH}/items/{{{{ $('Build upload').first().json.folderId }}}}", OD,
         body={"specifyBody": "json", "jsonBody": "={{ JSON.stringify({ parentReference: { id: $json.id } }) }}"}),
]

STUDIO_RESULT = ("={{ JSON.stringify({ status: $('Check result').first().json.status, "
                 "kind: $('Check result').first().json.kind, error: $('Check result').first().json.error, "
                 "title: $('Build upload').first().json.title, at: $now.toISO() }, null, 2) }}")
FAIL_MSG = ("={{ 'Studio upload failed (' + $('Check result').first().json.kind + '): ' "
            "+ $('Check result').first().json.error }}")
SHOT_URL = f"={BASE}/failures/{{{{ $('Build upload').first().json.folderName }}}}.jpg:/content"
browser_nodes = [
    node("Use Studio?", "n8n-nodes-base.if", 2.2, 1560, 300,
         {"conditions": equals("={{ $json.uploader }}", "browser"), "options": {}}),
    http("Dispatch upload", 1780, 80, "POST",
         f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/youtube-upload.yml/dispatches", GH,
         headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
         body={"specifyBody": "json", "jsonBody":
               "={{ JSON.stringify({ ref: '" + GITHUB_BRANCH + "', inputs: { post_b64: $json.postB64, "
               "video_url: $json.videoUrl, resume_url: $execution.resumeUrl, post_id: $json.folderName } }) }}"}),
    node("Wait for upload", "n8n-nodes-base.wait", 1.1, 2000, 80,
         {"resume": "webhook", "httpMethod": "POST", "limitWaitTime": True, "limitType": "afterTimeInterval",
          "resumeAmount": 45, "resumeUnit": "minutes", "options": {}}, webhookId=nid("wait-webhook")),
    code("Check result", 2220, 80, CHECK),
    node("Route", "n8n-nodes-base.switch", 3.2, 2440, 80, {"rules": {"values": [
        {"conditions": equals("={{ $json.route }}", r), "renameOutput": True, "outputKey": r}
        for r in ("ok", "unconfirmed", "post_failed", "error")]}, "options": {}}),
    http("Mark unconfirmed", 2660, 0, "PUT",
         f"={GRAPH}/items/{{{{ $('Build upload').first().json.folderId }}}}:/result.json:/content", OD,
         body={"specifyBody": "json", "jsonBody": STUDIO_RESULT}),
    http("Keep screenshot", 2660, 160, "PUT", SHOT_URL, OD, headers={"Content-Type": "image/jpeg"},
         body={"contentType": "binaryData", "inputDataFieldName": "data"}, onError="continueRegularOutput"),
    http("Mark failed", 2880, 160, "PUT",
         f"={GRAPH}/items/{{{{ $('Build upload').first().json.folderId }}}}:/result.json:/content", OD,
         body={"specifyBody": "json", "jsonBody": STUDIO_RESULT}),
    node("Short failed", "n8n-nodes-base.stopAndError", 1, 3100, 160, {"errorMessage": FAIL_MSG}),
    http("Keep screenshot ", 2660, 320, "PUT", SHOT_URL, OD, headers={"Content-Type": "image/jpeg"},
         body={"contentType": "binaryData", "inputDataFieldName": "data"}, onError="continueRegularOutput"),
    node("Upload failed", "n8n-nodes-base.stopAndError", 1, 2880, 320, {"errorMessage": FAIL_MSG}),
]
post_nodes += browser_nodes
conns = chain(["List ready", "Pick candidates", "List files", "Choose post", "Get post.json", "Build upload",
               "Use Studio?", "Dispatch upload", "Wait for upload", "Check result", "Route"])
link(conns, "Use Studio?", "Start YouTube upload", out=1)
chain(["Start YouTube upload", "Download video", "Upload video", "Save result.json", "Find posted folder",
       "Move to posted"], conns)
link(conns, "Route", "Save result.json", out=0)
link(conns, "Route", "Mark unconfirmed", out=1)
link(conns, "Route", "Keep screenshot", out=2)
chain(["Keep screenshot", "Mark failed", "Short failed"], conns)
link(conns, "Route", "Keep screenshot ", out=3)
chain(["Keep screenshot ", "Upload failed"], conns)
conns["Every 4 hours"] = {"main": [[{"node": "List ready", "type": "main", "index": 0}]]}
conns["Test now"] = {"main": [[{"node": "List ready", "type": "main", "index": 0}]]}

post_wf = {
    "name": "StreamVault: post Shorts",
    "nodes": post_nodes,
    "connections": conns,
    "settings": {"executionOrder": "v1", "timezone": "America/New_York", "saveManualExecutions": True},
    "pinData": {},
}

OUT.mkdir(parents=True, exist_ok=True)
for fname, wf in (("post-shorts.json", post_wf),):
    (OUT / fname).write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("wrote", OUT / fname)
