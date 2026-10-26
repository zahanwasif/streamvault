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


def chain(names):
    conns = {}
    for a, b in zip(names, names[1:]):
        conns.setdefault(a, {"main": [[]]})["main"][0].append({"node": b, "type": "main", "index": 0})
    return conns


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
const PRIVACY = 'public';      // 'private' or 'unlisted' to post without going live
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
return [{ json: {
  ...pick,
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

post_nodes = [
    note("About", -220, -260, 520, 380,
         "## StreamVault: post the next Short\n\n"
         "Runs every 4 hours. Looks at every folder in **OneDrive/StreamVault/ready/**, skips Shorts "
         "on hold, and picks the highest priority (set in the StreamVault app), then the best score, "
         "then the oldest. Uploads its video.mp4 to YouTube with the title, description and tags "
         "from post.json, writes result.json next to it and moves the folder to **posted/**.\n\n"
         "Nothing waiting = nothing happens. A failed upload leaves the Short in ready/ for next time.\n\n"
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
    http("Start YouTube upload", 1560, 300, "POST",
         "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status", YT,
         headers={"X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": "={{ $json.videoBytes }}"},
         body={"specifyBody": "json", "jsonBody": "={{ JSON.stringify($json.request) }}"},
         response={"fullResponse": True}),
    http("Download video", 1780, 300, "GET", "={{ $('Build upload').first().json.videoUrl }}",
         response={"responseFormat": "file"}, timeout=600000),
    http("Upload video", 2000, 300, "PUT", "={{ $('Start YouTube upload').first().json.headers.location }}", YT,
         headers={"Content-Type": "video/mp4"},
         body={"contentType": "binaryData", "inputDataFieldName": "data"}, timeout=1800000),
    http("Save result.json", 2220, 300, "PUT",
         f"={GRAPH}/items/{{{{ $('Build upload').first().json.folderId }}}}:/result.json:/content", OD,
         body={"specifyBody": "json", "jsonBody":
               "={{ JSON.stringify({ video_id: $json.id, url: 'https://youtube.com/shorts/' + $json.id, "
               "title: $('Build upload').first().json.title, privacy: $json.status?.privacyStatus, "
               "upload_status: $json.status?.uploadStatus, posted_at: $now.toISO() }, null, 2) }}"}),
    http("Find posted folder", 2440, 300, "GET", f"{BASE}/posted", OD),
    http("Move to posted", 2660, 300, "PATCH",
         f"={GRAPH}/items/{{{{ $('Build upload').first().json.folderId }}}}", OD,
         body={"specifyBody": "json", "jsonBody": "={{ JSON.stringify({ parentReference: { id: $json.id } }) }}"}),
]
order = ["List ready", "Pick candidates", "List files", "Choose post", "Get post.json", "Build upload",
         "Start YouTube upload", "Download video", "Upload video", "Save result.json", "Find posted folder",
         "Move to posted"]
conns = chain(order)
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
