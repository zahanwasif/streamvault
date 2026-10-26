# StreamVault queue: links in, Shorts posted on schedule

**For Claude:** the user asked you to set up the queue. Do the PC part (sections 1–3) on the PC, top to bottom. Section 4 happens in the user's browser in n8n; walk them through it.

## How it fits together

```
phone / laptop                     PC (when it's on)                         n8n (in the cloud, always on)
──────────────                     ─────────────────                         ─────────────────────────────
"Add videos" n8n form ──┐
OneDrive app: drop      ├─► OneDrive/StreamVault/queue/
  links.txt or videos ──┘            │
                                     ▼  every 30 min, one item at a time
                              clipper/run_queue.py
                                fetch.py → Claude Code (shorts-clipper) → export_posts.py
                                     │
                                     ▼
                              OneDrive/StreamVault/ready/<post>/video.mp4 + post.json
                                     │  (OneDrive desktop app syncs it to the cloud)
                                     ▼
                                                                 at 12:00, 16:00, 19:00 US Eastern:
                                                                 oldest ready/ post → YouTube
                                                                 → posted/<post>/ + result.json
```

The PC only has to be on to make Shorts. Posting runs in n8n, so it keeps going while the PC is off, as long as `ready/` has Shorts in it. At 3 a day, one 15-Short VOD lasts 5 days.

**n8n must not run on this PC**, or posting stops whenever the PC is off. Use n8n Cloud or n8n on an always-on server.

### OneDrive folders (`OneDrive/StreamVault/`)

| Path | Who writes it | What it holds |
|---|---|---|
| `queue/` | you, and the n8n form | `.txt`/`.url` files of links (one per line, optional `--section 1:00:00-2:30:00`) and video files |
| `queue/done/` | PC worker | link files whose links have all been handled |
| `ready/<post id>/` | PC worker | `video.mp4` + `post.json` (title, description, tags) waiting to be posted |
| `posted/<post id>/` | n8n | the same, plus `result.json` with the YouTube link |
| `status.md` | PC worker | queue table: what's done, what failed and why, how many Shorts are waiting |

Post ids are `<export time>_<rank>_<source slug>`, so they post oldest source first, best Short first. To change the order, rename folders. To hold a Short back, move it out of `ready/`.

## 1. OneDrive on the PC
- The OneDrive desktop app must be signed in to the account with the 1 TB plan and syncing. Files On-Demand can stay on.
- The worker uses `%OneDrive%\StreamVault` and creates `queue\`, `ready\` and `posted\` there. For any other folder, set `setx STREAMVAULT_ONEDRIVE "D:\OneDrive\StreamVault"`.
- Only finished Shorts and status files go to OneDrive. Downloads and transcripts stay in `C:\StreamVault`.

## 2. Claude Code for headless runs
The worker runs `claude -p` (headless Claude Code) for each source, using the user's Claude login.
- `claude --version` must work in a new terminal, and `claude` must be signed in. Run it interactively once in `C:\StreamVault` and accept the folder-trust prompt.
- Each run is allowed `Read`, `Write`, `Edit`, `Glob`, `Grep`, sub-agents, and only `uv run`, `ffmpeg`, `ffprobe`, `mkdir` and `ls` in Bash. Change this with `--tools` or the `STREAMVAULT_CLAUDE_TOOLS` env var.
- Transcripts of other people's videos are untrusted input to that run. The allowlist limits what it can do but isn't a sandbox, so only queue sources you'd clip by hand.

## 3. The queue worker
Test it first:
```bash
cd /c/StreamVault
uv run --project video-use python clipper/run_queue.py --dry-run      # lists what's queued
uv run --project video-use python clipper/run_queue.py                # processes the queue, then exits
```
Then schedule it every 30 minutes. `conhost --headless` keeps a console window from popping up:
```powershell
schtasks /Create /TN "StreamVault queue" /SC MINUTE /MO 30 /F `
  /TR "conhost.exe --headless C:\StreamVault\clipper\run_queue.cmd"
```
- A lock makes later starts exit while a long VOD is still running.
- A failed item retries on the next run and gives up after 2 tries. The reason shows in `status.md`, and the full log is in `.worker\logs\<n>.log`.
- Add `--delete-video` to the `/TR` command (after `run_queue.cmd`) to delete each downloaded VOD once its Shorts are exported. Transcripts are kept.
- A source that already has `shorts/<slug>/index.md` isn't re-clipped. Its unexported Shorts are just exported.
- Shorts flagged in the self-check (`"flagged"` in their meta JSON) stay on the PC for you to review. Clear the flag, then export them with `clipper/export_posts.py shorts/<slug>`.

## 4. n8n (in the browser)

### 4a. Import the workflows
In n8n, go to **Workflows → Import from File** and import both files:
- `n8n/post-shorts.json`: posts at the slot times.
- `n8n/add-videos.json`: a web form that adds links to the queue.

### 4b. Microsoft OneDrive credential
1. In n8n, create a **Microsoft Drive OAuth2 API** credential and copy its OAuth Redirect URL.
2. Register an app at https://entra.microsoft.com (App registrations → New registration):
   - Supported account types: **any organizational directory and personal Microsoft accounts**.
   - Redirect URI: **Web**, the URL from step 1.
3. Under Certificates & secrets, create a new client secret.
4. Paste the Application (client) ID and the secret *value* into n8n, then click **Connect** and sign in with the OneDrive account.

### 4c. YouTube credential
1. At https://console.cloud.google.com, create a project and enable **YouTube Data API v3**.
2. Set up the OAuth consent screen: External. Then set Publishing status to **In production**. In "Testing", Google expires the sign-in after 7 days and posting stops. Unverified is fine for your own channel; you'll click past a warning when connecting.
3. Create an OAuth client: type **Web application**, with the redirect URL from n8n's **YouTube OAuth2 API** credential.
4. Paste the client ID and secret into n8n, then click **Connect** and sign in with the channel's Google account.

### 4d. Wire up and turn on
- Open every HTTP node with a warning and pick the matching credential: OneDrive for the Graph calls, YouTube for *Start YouTube upload* and *Upload video*.
- **Posting times:** the cron in *Posting times* (default `0 12,16,19 * * *`), in the workflow's time zone (Settings → Timezone, default America/New_York).
- **Privacy, category, language:** the top of the *Build upload* Code node.
- Click **Test now** → *Execute workflow* with one Short in `ready/`, and check it reaches YouTube and moves to `posted/`. Then toggle both workflows **Active**.
- Open *Add videos* → Production URL and bookmark it on the phone. Anyone with that URL can add links, so keep it private or turn on the form's Basic Auth option.

### 4e. YouTube API audit (required for public posts)
Google locks every video uploaded through the Data API from an **unaudited** project to **private**, whatever the workflow asks for. The upload still works, so the test in 4d will succeed but the video will be private.
- Submit the audit: https://support.google.com/youtube/contact/yt_api_form ("YouTube API Services – Audit and Quota Extension"). Describe it as a personal tool that uploads your own channel's Shorts on a schedule. Approval can take days to weeks.
- Until it's approved, uploads land as private. Either leave the workflow off and keep using the Studio flow in the shorts-clipper skill, or let it upload and flip each video to public in Studio.
- An unverified channel can upload 10 videos a day; verify it at https://www.youtube.com/verify.

## Troubleshooting
- **Nothing posts:** check that `ready/` has folders with both files. A folder whose `video.mp4` is still syncing is skipped until its size matches `post.json`. Also check the n8n executions list for errors.
- **The same Short keeps failing:** n8n's error is in the execution. If it's the file itself, move that folder out of `ready/`.
- **`result.json` but still in `ready/`:** it was uploaded but the move failed. n8n won't upload it again; move it to `posted/` by hand.
- **Worker does nothing:** run it by hand and read the output. Also look at `.worker\task.log` and `status.md`.
