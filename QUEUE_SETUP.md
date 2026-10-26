# StreamVault: desktop app (PC) + n8n poster (cloud)

**For Claude:** the user asked you to set this up. Do sections 1–2 on the PC, top to bottom. Section 3 happens in the user's browser in n8n; walk them through it.

## Two independent processes

```
PC: StreamVault desktop app (app/)                          n8n (in the cloud), every 4 hours
──────────────────────────────────                          ─────────────────────────────────
Add tab: links / video files, priority                      list OneDrive/StreamVault/ready/
   │                                                        skip Shorts on hold or still syncing
   ▼  engine: clipper/run_queue.py, one video at a time     pick: priority → score → oldest
download → VoiceStudio transcript → pack → signals          upload to YouTube with post.json's
→ Claude Code picks + renders Shorts → export                 title, description, tags
   │                                                        write result.json, move to posted/
   ▼
OneDrive/StreamVault/ready/<post id>/video.mp4 + post.json  ◄── the only thing the two share
   ▲
Shorts tab: edit title/description/tags, priority, Hold/Release
```

- The PC only has to be on to make Shorts.
- n8n posts whatever is in `ready/` whether the PC is on or not. At one post every 4 hours, that's 6 a day, so a 15-Short video lasts about 2½ days.
- **n8n must not run on this PC**, or posting stops whenever the PC is off. Use n8n Cloud or an always-on server.

### The OneDrive folder (`OneDrive/StreamVault/`)

| Path | Written by | Holds |
|---|---|---|
| `ready/<post id>/` | the app | `video.mp4` + `post.json` (title, description, tags, `priority`, `score`, `hold`, `flag`) |
| `posted/<post id>/` | n8n | the same, plus `result.json` with the YouTube link |

Shorts that the self-check flagged arrive **on hold**, with the reason in `flag`. Release them in the app's Shorts tab once you've looked at them.

## 1. PC prerequisites
Finish `PC_SETUP.md` first, so the pipeline, VoiceStudio and the video-use environment all work. Then:
- **Node.js 22+** for the app. `node --version` should show it.
- **Claude Code:** `claude --version` must work and be signed in. Run `claude` interactively once in `C:\StreamVault` and accept the folder-trust prompt. The app runs it headless (`claude -p`) for each video.
- **OneDrive:** the desktop app must be signed in to the 1 TB account and syncing. The app writes to `%OneDrive%\StreamVault` by default.
- **VoiceStudio** must be running with WhisperX on CUDA (`PC_SETUP.md` §6). The app's Settings → Checks shows whether it answers.

## 2. The desktop app
```bash
cd /c/StreamVault/app
npm install
npm start
```
For an installer, run `npm run dist`; `app/dist/` gets a Windows setup .exe. The installed app looks for the repo at `C:\StreamVault`; change it under Settings → StreamVault folder.

**Settings tab**
- **Output folder:** `%OneDrive%\StreamVault` by default. A folder outside OneDrive works on the PC, but n8n in the cloud can't read it, and the check says so.
- **VoiceStudio URL:** default `http://127.0.0.1:3900`.
- **Minimum score / max Shorts per video:** 0 means the skill's defaults (7 and 15).
- **Default priority, delete downloaded videos after export** (transcripts are kept), **start at Windows sign-in.**
- **Checks:** VoiceStudio, Claude Code, uv, and both folders.

**Using it**
- **Add:** paste links (an optional "only this part" range like `1:00:00-2:30:00`), or drop video files, and pick High, Normal or Low.
- **Queue:**
  - One video runs at a time, highest priority first.
  - Each job shows its step: Download → Transcribe → Pack → Signals → Find & render → To OneDrive.
  - Log shows the full output, and the live line shows what Claude is doing.
  - A failed job retries once after 10 minutes. Retry now, Cancel and Remove are on each row.
  - Changing a job's priority also re-prioritises its Shorts that are still waiting.
- **Shorts:**
  - Every Short in `ready/` and `posted/`, in the same order n8n will post them. The first one is marked **Next up**.
  - Edit the title, description and tags, set the priority, then **Save**. n8n posts whatever is in `post.json` at the time.
  - **Hold** keeps n8n away from a Short; **Release** gives it back.
  - Posted Shorts show their YouTube link.
- **Closing the window** keeps the app running in the tray. Quit from the tray menu. Quitting mid-video stops it, and it retries from the start next time; cached transcripts aren't redone.

**How the app talks to the engine:** it starts `uv run --project video-use python clipper/run_queue.py --watch --parent-pipe`. Commands go in as files in `.worker/requests/`. State is in `.worker/state.json`, and per-job logs are in `.worker/logs/`. The engine exits when the app does. The same engine also runs without the app (`clipper/run_queue.py` with no `--watch` processes whatever is queued, then exits).

**Safety:** transcripts of other people's videos are input to the headless Claude run. Its tools are limited (`Read`/`Write`/`Edit`/`Glob`/`Grep`, sub-agents, and only `uv run`, `ffmpeg`, `ffprobe`, `mkdir` and `ls` in Bash), but that isn't a sandbox. Only add videos you'd clip by hand.

## 3. n8n (in the browser)

### 3a. Import
In n8n, go to **Workflows → Import from File** and pick `n8n/post-shorts.json`. To change the workflow, edit `n8n/build_workflows.py` and run `python n8n/build_workflows.py`, or edit it in n8n and export it back.

### 3b. Microsoft OneDrive credential
1. In n8n, create a **Microsoft Drive OAuth2 API** credential and copy its OAuth Redirect URL.
2. Register an app at https://entra.microsoft.com (App registrations → New registration):
   - Supported account types: **any organizational directory and personal Microsoft accounts**.
   - Redirect URI: **Web**, the URL from step 1.
3. Under Certificates & secrets, create a new client secret.
4. Paste the Application (client) ID and the secret *value* into n8n, then click **Connect** and sign in with the OneDrive account.

### 3c. YouTube credential
1. At https://console.cloud.google.com, create a project and enable **YouTube Data API v3**.
2. Set up the OAuth consent screen: External. Then set Publishing status to **In production**. In "Testing", Google expires the sign-in after 7 days and posting stops. Unverified is fine for your own channel; you'll click past a warning when connecting.
3. Create an OAuth client: type **Web application**, with the redirect URL from n8n's **YouTube OAuth2 API** credential.
4. Paste the client ID and secret into n8n, then click **Connect** and sign in with the channel's Google account.

### 3d. Wire up and turn on
- Open every HTTP node with a warning and pick the matching credential: OneDrive for the Graph calls, YouTube for *Start YouTube upload* and *Upload video*.
- **Schedule:** *Every 4 hours* is cron `0 */4 * * *` in the workflow's time zone (Settings → Timezone, default America/New_York). It fires at 00:00, 04:00, 08:00 and so on. To post at other times, edit the cron, e.g. `0 1,5,9,13,17,21 * * *`.
- **Privacy, category, language:** the top of the *Build upload* Code node.
- Click **Test now** → *Execute workflow* with a Short in `ready/`, and check it reaches YouTube and moves to `posted/`. Then toggle the workflow **Active**.

### 3e. YouTube API audit (required for public posts)
Google locks every video uploaded through the Data API from an **unaudited** project to **private**, whatever the workflow asks for. The upload still works, so the test in 3d will succeed but the video will be private.
- Submit the audit: https://support.google.com/youtube/contact/yt_api_form ("YouTube API Services – Audit and Quota Extension"). Describe it as a personal tool that uploads your own channel's Shorts on a schedule. Approval can take days to weeks.
- Until it's approved, uploads land as private; flip them to public in Studio, or keep the workflow off.
- An unverified channel can upload 10 videos a day; verify it at https://www.youtube.com/verify.

## Troubleshooting
- **Engine "Stopped" in the app:** Settings → Engine output shows why. Usually it's a wrong StreamVault folder, `uv` missing from PATH, or no output folder set.
- **Job fails at Transcribe:** VoiceStudio isn't running, or the WhisperX model isn't loaded.
- **Job fails at Find & render:** open its Log. `FAILED.txt` reasons and Claude errors show there.
- **Nothing posts:** check that the Shorts tab has something under Waiting (not on hold) and that it's synced to OneDrive online. Then check the n8n executions list.
- **The same Short keeps failing in n8n:** Hold it in the app and read the error in the n8n execution.
- **`result.json` but still in `ready/`:** it was uploaded but the move failed. n8n won't upload it again; move it to `posted/` by hand.
