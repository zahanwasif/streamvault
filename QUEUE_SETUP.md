# Install StreamVault

**For Claude:** if the user asks you to set StreamVault up, point them to the installer and the app's **Setup** tab; everything below happens there. The manual steps in `PC_SETUP.md` are a fallback.

## Two independent processes

```
PC: StreamVault app                                         n8n (in the cloud), every 4 hours
───────────────────                                         ─────────────────────────────────
Add tab: links / video files, priority                      list OneDrive StreamVault/ready/
   │                                                        skip Shorts on hold or still syncing
   ▼  engine: clipper/run_queue.py, one video at a time     pick: priority → score → oldest
download → VoiceStudio transcript → pack → signals          upload with post.json's title,
→ Claude Code picks + renders Shorts → outbox/                description, tags: through Studio
   │                                                          on GitHub Actions (until the API
   ▼  the app's uploader (Microsoft Graph, chunked)            audit clears), or the YouTube API
online OneDrive: StreamVault/ready/<post id>/               write result.json, move to posted/
   video.mp4 + post.json  ◄── the only thing the two share (in the cloud, not on the PC)
   ▲
Shorts tab: edit title/description/tags, priority, Hold/Release (edits go straight to OneDrive)
```

- The PC only has to be on to make and upload Shorts. n8n posts whatever is in `ready/`, whether the PC is on or not.
- At one post every 4 hours that's 6 a day, so a 15-Short video lasts about 2½ days.
- **n8n must not run on this PC**, or posting stops whenever the PC is off. Your n8n.veleads.io is fine.

## 1. Install
- **From GitHub Actions:** the *Build StreamVault installer* workflow builds `StreamVault-Setup-<version>.exe` on every change; download it from the run's artifacts. A tag like `app-v1.1.0` also publishes it as a Release.
- **Or build it yourself:** on a Windows PC, run `cd app && npm ci && npm run dist`. You need the repo cloned with `--recurse-submodules`.

Run the installer. It isn't code-signed, so Windows SmartScreen asks once: **More info → Run anyway**. The app installs per user, and the installer carries the scripts, so you don't need git or a clone.

## 2. The Setup tab
The app opens on **Setup** until every required step is green. Each step checks itself and has a **Re-check** link. Nothing installs or changes until you click its button.

| # | Step | What you do | What the app does |
|---|---|---|---|
| 1 | **Workspace folder** | Pick a drive with space (default `C:\StreamVault`) and click **Copy scripts**. | Copies `clipper/`, `uploader/`, the shorts-clipper skill and the patched `video-use` there. After each app update it refreshes them, and never touches `sources/`, `shorts/`, `inbox/` or `outbox/`. |
| 2 | **Tools** | Click **Install** on anything missing. | Runs the official installer: winget for uv, ffmpeg, Node.js and Chrome; `uv tool install` for yt-dlp; Claude's and VoiceStudio's own scripts. It then re-reads PATH, so no restart is needed. |
| 3 | **Python environment** | Click **Set up Python environment**. | Runs `uv sync` in `video-use` and links the video-use skill for Claude Code. A junction you made by hand to the same folder is kept. |
| 4 | **Claude sign-in** | Click **Sign in to Claude**, finish in the browser, then Re-check. | Runs `claude auth login` in its own window. |
| 5 | **VoiceStudio model** | In VoiceStudio, pick **WhisperX (large-v3)** on **CUDA**. Click **Allow 1-hour files**, restart VoiceStudio, then click **Test transcription**. | Sets `OMNIVOICE_ASR_TRANSCRIBE_TIMEOUT_S=3600` and sends 2 s of audio through the transcription endpoint. |
| 6 | **n8n server** | Enter your n8n address and an API key (n8n → Settings → n8n API → Create an API key). | Tests the API and shows the OAuth redirect URL, which you need for steps 7 and 10. |
| 7 | **Microsoft account (OneDrive)** | Register one Azure app, following the on-screen list: account types, the redirect URL from step 6, *Allow public client flows: Yes*, a client secret. Paste the client ID and secret, then **Connect OneDrive** and enter the code. | Signs in with a device code. Shorts then upload to your online OneDrive's `StreamVault/`. The secret is only passed to n8n. |
| 8 | **GitHub** | Create a fine-grained token for this repo only (Actions + Secrets: read and write) and save it. | Checks the token, the repo and that `youtube-upload.yml` is on the default branch. |
| 9 | **YouTube sign-in** | **1. Open Chrome**, sign in to YouTube Studio as the channel, then **2. Save YouTube session**. | Reads the youtube.com/google.com cookies from that Chrome window and stores them as the repo secret `YT_SESSION`. It also stores the token as `SECRETS_PAT`, so each run keeps the session fresh. |
| 10 | **YouTube API** (optional) | Only after Google approves your API audit: a Google OAuth client (Web, the redirect URL from step 6, consent screen *In production*). | Adds the API path to the workflow. |
| 11 | **n8n posting workflow** | Choose how often and the time zone, then **Create workflow in n8n**. Click **Connect OneDrive in n8n** (and YouTube, if set) and sign in there once. Then **Activate**. | Creates the credentials and the "StreamVault: post Shorts" workflow from the built-in template (your folder, repo, schedule). **Update** re-sends it after you change something. Shows recent runs. |

Notes:
- **Secrets** (n8n API key, Azure secret, GitHub token, Google secret, the OneDrive sign-in) are encrypted with Windows' DPAPI in the app's data folder. They're never shown again in full.
- **Step 8 needs the uploader workflow on `main`:** `.github/workflows/youtube-upload.yml` must be on the repo's default branch. The check tells you if it isn't, which means this branch hasn't been merged yet.
- **Why n8n's OAuth sign-ins aren't automatic:** they're browser sign-ins that only n8n's own page can run. That's why step 11 opens it for you.

## 3. Using the app
- **Add:** paste links (an optional "only this part" range like `1:00:00-2:30:00`) or drop video files, and pick High, Normal or Low.
- **Queue:**
  - One video runs at a time, highest priority first.
  - Each job shows its step: Download → Transcribe → Pack → Signals → Find & render → To OneDrive.
  - **Log** shows the full output, and the live line shows what Claude is doing.
  - A failed job retries once after 10 minutes. **Retry now**, **Cancel** and **Remove** are on each row.
  - Changing a job's priority also changes its Shorts that are still waiting.
- **Shorts:** filters for Uploading, Waiting, On hold, Posted and Needs a look.
  - **Waiting** is in n8n's order; the first one is **Next up**.
  - Edit the title, description and tags, set the priority, then **Save**. **Hold** keeps n8n away; **Release** gives it back.
  - **Needs a look** lists uploads that failed or were never confirmed, with Studio's screenshot, and **Try again** / **Mark posted** buttons.
- **Uploads:**
  - Finished Shorts wait in `<workspace>\outbox\` until they're uploaded, then are deleted there. The originals stay in `shorts/`.
  - A failed upload retries after 1, 2, 5, then every 15 minutes.
  - A Short n8n already posted is never uploaded again.
- **Settings:** minimum score and max Shorts per video (0 = the skill's defaults, 7 and 15), default priority, delete downloaded videos after export, start at Windows sign-in, and the engine's output.
- **Closing the window** keeps the app running in the tray. Quit from the tray menu.

## The Studio uploader (until the API audit clears)
n8n picks the Short and starts the GitHub Actions workflow `youtube-upload.yml`. That run uploads through YouTube Studio with Playwright (`uploader/studio_upload.py`) and reports back, and n8n then moves the Short to `posted/`.

- **Terms:** automating Studio is against YouTube's terms, and the channel can be flagged for it.
- **Breakage:** Studio changes can break the script. All its selectors are in `SELECTORS` at the top of `studio_upload.py`.
- **Sign-outs:** each run comes from a different GitHub datacenter IP, so Google may sign the session out. When it does (**Needs a look** shows `session`), repeat Setup step 9 (YouTube sign-in).
- **Public logs:** the repo is public, so its Actions logs are too. The workflow masks the download link and the n8n callback URL, and failure screenshots go to OneDrive `failures/`, never to a public artifact. Making the repo private keeps the uploader itself private; private repos get 2,000 free Actions minutes a month, and 6 uploads a day use about 700.
- **Test by hand:** Actions → *YouTube upload (Studio)* → Run workflow, with **dry_run** ticked, `post_b64` = `echo '{"title":"Test","description":"test","tags":["test"]}' | base64`, and `video_url` = a direct link to a short mp4. It leaves a private draft in Studio.
- **After the audit:** set **Upload through** to *YouTube API* in step 11 and click **Update**. Uploads then use the Data API, with no browser and no session to keep alive.

| Failure | What happens |
|---|---|
| Signed out (`session`) | The Short stays in `ready/` and goes out on the next run once you redo step 9. |
| Daily upload limit (`limit`) | It waits for the next run. Verifying the channel at https://www.youtube.com/verify raises the limit. |
| Copyright claim (`post`) | Left private in Studio. The Short goes under **Needs a look**, and n8n skips it. |
| No report within 45 min | Marked *unconfirmed* and never re-uploaded. Check Studio, then **Mark posted** or **Try again**. |
| Studio changed (`studio`) | The screenshot in OneDrive `failures/` shows where it stopped. |

## Troubleshooting
- **A Setup step stays red:** its message says why, and **Re-check** runs it again. Installer output is in the step's log box.
- **Engine "Stopped":** the sidebar says why, usually an unfinished Setup step. Settings → Engine output has the details.
- **Job fails at Transcribe:** VoiceStudio isn't running, or no model is loaded (Setup step 5, **Test transcription**).
- **Job fails at Find & render:** open its **Log**. Claude errors and `FAILED.txt` reasons show there.
- **Shorts stuck under Uploading:** the sidebar box shows the error. "Connect OneDrive" means the sign-in expired: redo Setup step 7.
- **Nothing posts:** check that Shorts has something under Waiting (not on hold) and that Setup step 11 says *active*. The recent runs in step 11, or n8n's Executions, show errors.
- **You changed the repo, folder or schedule:** Setup step 11 → **Update workflow in n8n**.
