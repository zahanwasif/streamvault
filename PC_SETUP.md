# StreamVault: setup guide for the desktop PC (RTX 3080, Ryzen 7 5700X3D)

**For Claude:** the user asked you to set up StreamVault on this PC. Work through this file top to bottom, and keep the task list current.

## What StreamVault is

You paste a YouTube or Twitch URL, or drop videos in `inbox/`. Claude finds the funny, romantic, engaging and hype moments, renders 9:16 YouTube Shorts with captions, and saves them to `shorts/<slug>/` with a metadata JSON per clip. That JSON feeds the posting queue (`QUEUE_SETUP.md`).

It's built on [video-use](https://github.com/browser-use/video-use), plus the `shorts-clipper` skill and the scripts in `clipper/`. Everything except real Scribe transcription was built and tested on the laptop on 2026-10-25.

**User preferences:**
- Fully automatic: no approval stops.
- Layout chosen per video.
- Every clip scoring 7/10 or higher, up to 15 per video.
- Content is a mix of gaming with a facecam and talking heads.

**Why move to this PC:** it has an RTX 3080, so transcription can run locally and free through VoiceStudio (WhisperX on CUDA) instead of the paid ElevenLabs Scribe.

## 0. Location

Use `C:\StreamVault`. Don't use OneDrive: downloaded streams and `.venv` are gigabytes. It's a clone of https://github.com/zahanwasif/streamvault, with video-use as a submodule pinned to the commit the Windows patch was made against:
```bash
git clone --recurse-submodules https://github.com/zahanwasif/streamvault.git /c/StreamVault
```
If it was cloned without `--recurse-submodules`, run `git submodule update --init` inside it. `sources/`, `shorts/` and `inbox/` are git-ignored apart from a `.gitkeep`, and so is `.env`.

## 1. Prerequisites
- Check: `git`, `uv`, `node --version` (VoiceStudio prefers 22+), and `nvidia-smi` (driver installed, GPU visible).
- Install anything missing with winget. Ask the user before installing.

## 2. video-use
The submodule is already at commit `43cfc56`. Apply the Windows patch and install the dependencies:
```bash
cd /c/StreamVault/video-use && git apply ../clipper/video-use-windows.patch && uv sync
```
The patch leaves the submodule with local changes; `git status` shows `video-use (modified content)`. That's expected, so don't commit inside the submodule. If you later update video-use and the patch no longer applies, make the two changes by hand:
- **`render.py`:** the subtitles path must use `.as_posix()`. Windows backslashes break the ffmpeg filtergraph and captions fail.
- **`timeline_view.py`:** add `C:/Windows/Fonts/consola.ttf` and `C:/Windows/Fonts/arial.ttf` to `FONT_CANDIDATES`.

## 3. Tools and environment
- `winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements`
- `uv tool install "yt-dlp[default]"`. The `[default]` extra brings yt-dlp-ejs, which YouTube downloads need. `fetch.py` passes `--js-runtimes node`.
- `setx PYTHONUTF8 1`. Without it, the helpers crash printing "→" on a cp1252 console.
- New PATH and environment values only reach processes started afterwards. `clipper/_env.py` already finds ffmpeg itself and forces UTF-8 output, so the scripts work before a restart.

## 4. Register skills
- video-use (global, as a junction so `helpers/` stays next to `SKILL.md`):
  `New-Item -ItemType Junction -Path "$env:USERPROFILE\.claude\skills\video-use" -Target "C:\StreamVault\video-use"`
- shorts-clipper is project-scoped (`C:\StreamVault\.claude\skills\`). It loads whenever Claude runs in `C:\StreamVault`.

## 5. ElevenLabs key (optional fallback)
- Ask the user to paste a key, and write it to `video-use/.env` as `ELEVENLABS_API_KEY=...`.
- The laptop's key was pasted into a chat, so suggest creating a fresh one.
- Check it with `GET https://api.elevenlabs.io/v1/user`: 200 means OK. Never echo the key.

## 6. VoiceStudio (local GPU transcription)
- **Install:** the Windows installer is `irm https://voicestudio.sh/install | iex`, or there's a `.exe` under Releases at https://github.com/debpalash/VoiceStudio. **Ask the user before running the installer script.**
- **Model:** in the app, go to Model Catalogue → ASR, select **WhisperX** (`large-v3`) and let it download. Confirm it runs on CUDA (float16), not CPU.
- **Long files:** the whole-file timeout `OMNIVOICE_ASR_TRANSCRIBE_TIMEOUT_S` defaults to 300 s. Set it as a user environment variable (e.g. `3600`) and restart VoiceStudio.
- **Optional speaker labels:** these need a Hugging Face token, plus accepting the licenses for `pyannote/speaker-diarization-3.1` and `pyannote/segmentation-3.0`. Speaker labels are *not* returned by the transcription endpoint below, so skip this unless it's needed later.
- **Check:** `curl http://127.0.0.1:3900/.well-known/voicestudio-speech` should answer.

**API we use:** `POST http://127.0.0.1:3900/v1/audio/transcriptions` (OpenAI-compatible), with these form fields:
- `file`
- `model=whisper-1` (means "the active engine")
- `response_format=verbose_json`
- `timestamp_granularities[]=word`
- optional `language` and `prompt`

It returns `words: [{word, start, end}]` and `segments`. No credentials are needed on loopback. Source: `backend/api/routers/openai_compat.py`.

**What it doesn't give us, compared with Scribe:**
- no audio events such as laughter or applause
- no speaker IDs in this endpoint
- "um"/"uh" are mostly normalized away

A `prompt` full of fillers ("Umm, uh, like, you know... haha") nudges Whisper toward keeping them.

## 7. Build: switchable transcription engine
In `clipper/transcribe_long.py`, add `--engine local|scribe`. The default comes from the `CLIPPER_ASR` env var, falling back to `local`.

**`local`:**
- Extract 16 kHz mono audio (reuse video-use `extract_audio` and `peak_dbfs`).
- Split it into 10-min chunks and send them **one at a time** (single GPU) to the endpoint above.
- Cache each chunk response in `transcripts/.chunks/<stem>.local.partNN.json`.
- Merge with time offsets.
- Convert to Scribe's shape, so `pack_transcripts.py`, `render.build_master_srt`, `words.py` and `signals.py` keep working unchanged:
  - each word becomes `{"text", "start", "end", "type": "word", "speaker_id": null}`
  - add a `{"type": "spacing", "start": prev_end, "end": next_start}` entry between words
- Write to the standard `transcript_path(edit_dir, video)`. Record `"engine": "voicestudio-whisperx"` in the JSON.

**`scribe`:** the current code path, unchanged.

**Elsewhere:**
- `signals.py`: when the transcript engine is local, say in `signals.md` that audio events are unavailable. Loudness spikes and chat bursts still work.
- shorts-clipper `SKILL.md`: document the engine switch. Use Scribe when the user asks for it, or when laughter detection matters more than cost.

## 8. Verify
1. **Renderer:** render a synthetic test clip (ffmpeg `testsrc2` plus `sine`, with a fake transcript JSON) through `clipper/render_short.py` in all four layouts (`center`, `split`, `blur`, `fit`). Each should be 1080x1920 at the expected duration, and `clipper/sheet.py` should show readable captions.
2. **Local transcription:** run `fetch.py https://www.youtube.com/watch?v=jNQXAC9IVRw` (19 s), then `transcribe_long.py --engine local`, then `pack_transcripts.py`. Check the word times look right, and report how long it took.
3. **End to end:** the user pastes a real stream URL. Run the shorts-clipper skill fully automatically and report the transcription speed (minutes per hour of audio).

## 9. Queue and scheduled posting
Once the pipeline works, set up the OneDrive link queue and n8n posting: follow `QUEUE_SETUP.md`.

## Pitfalls already solved (don't redo)
- **Captions failing on Windows:** the `.as_posix()` patch in step 2 fixes this.
- **Console crashes on "→":** `PYTHONUTF8=1`, plus the UTF-8 handling in `_env.py`.
- **Tiny timeline labels:** the Windows fonts added in step 2.
- **YouTube downloads failing:** yt-dlp needs the `[default]` extras and `--js-runtimes node`.
- **Download progress flooding the context:** `fetch.py` uses `--newline --progress-delta 30`.
- **Wrong source key:** `render_short.py` passes the layout through the EDL `grade` field and keys sources by the video's stem, because `build_master_srt` looks up `transcripts/<stem>.json`.
