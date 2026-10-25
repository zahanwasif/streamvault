---
name: shorts-clipper
description: Turn a YouTube/Twitch URL or a folder of videos into finished 9:16 YouTube Shorts of the funniest, most romantic and most engaging moments, saved to StreamVault/shorts/. Use whenever the user pastes a youtube.com, youtu.be or twitch.tv link, drops videos in inbox/, or asks to clip, find highlights, or make Shorts from a video or stream.
---

# Shorts clipper

Fully automatic: URL or folder in, finished Shorts out. **Do not stop to ask for approval**: the user chose hands-off mode, which overrides video-use's "confirm strategy first" rule for this workflow. Stop only for hard failures you cannot fix: a download that needs login cookies, a Scribe 401, or silent audio.

Built on video-use (`video-use/SKILL.md`). Its production rules still apply: cut on word boundaries, pad edges, subtitles last, never re-transcribe.

## Settings

| Setting | Value |
|---|---|
| Minimum score to render | **7** / 10 |
| Max Shorts per source | **15** |
| Clip length | **15–60 s** (go up to 90 s only for a story whose payoff needs it) |
| Parallel renders | 3 |

The user can override any of these in their message ("only 9s and up", "max 5").

## Commands

Run everything from `C:\Users\zahan\OneDrive\Desktop\work\StreamVault` with the video-use environment:

```bash
PY="uv run --project video-use python"
$PY clipper/fetch.py <url | file | folder> [--section 1:00:00-2:30:00] [--cookies-from-browser chrome]
$PY clipper/transcribe_long.py sources/<slug> [--language en]
$PY video-use/helpers/pack_transcripts.py --edit-dir sources/<slug>/edit
$PY clipper/signals.py sources/<slug>
$PY clipper/words.py sources/<slug> <start> <end>
$PY clipper/render_short.py sources/<slug>/edit/clips/*.json --jobs 3
$PY clipper/sheet.py shorts/<slug>
```

## Pipeline (per source)

### 1. Ingest
- URL: `fetch.py <url>`. A folder (default `inbox/`) or file: `fetch.py <path>`. Each source becomes `sources/<slug>/` with `info.json` (title, uploader, url, `video` path). Use the printed `SOURCE` lines.
- The user may give a time range ("only the first 2 hours"); pass it as `--section`.
- Download fails because the video is subscriber-only, members-only or age-gated: tell the user to rerun with `--cookies-from-browser chrome` (or their browser). Don't retry blindly.

### 2. Transcribe + signals
Run `transcribe_long.py`, then `pack_transcripts.py`, then `signals.py`. Transcripts are cached; never delete them. Long VODs are chunked automatically. Scribe bills per audio hour, so mention the hours transcribed in the final report.

### 3. Find candidate moments
Inputs: `edit/takes_packed.md` (phrases with `[start-end]` seconds) and `edit/signals.md` (loudness spikes, Scribe audio events, chat bursts).

- Source **≤ 90 min**: read both yourself.
- Source **> 90 min**: spawn one sub-agent per 60-min window, **all in parallel in a single message**, with windows overlapping by 2 min. Each brief is self-contained:
  > "Read `<abs path>/takes_packed.md`, lines whose `[start-...]` falls in `<a>`–`<b>` s, plus the `<a>`–`<b>` entries in `<abs path>/signals.md`. Using the rubric below, return ONLY a JSON array of candidates `{start, end, category, hook, score, reason}`. Do not render, do not ask questions." Paste the rubric into the brief.

Signals point at moments; the transcript says what happens there. A loudness spike or chat burst with no transcript explanation may be a gameplay moment: check it with one frame (`ffmpeg -ss <t> -i <video> -frames:v 1 -vf scale=960:-2 edit/verify/t<t>.png`, then Read it) before scoring it.

**Rubric** (score 1–10 for how well the clip works on its own, with no context, to a stranger scrolling Shorts):

| Category | What it looks like |
|---|---|
| `funny` | Punchline, roast, absurd line, fail, someone breaking down laughing. `(laughter)` events and laugh-heavy chat bursts are strong evidence. |
| `romantic` | Flirting, compliments, blushing, a sweet or tender exchange, a couple teasing each other, "rizz" moments. |
| `engaging` | Hot take, surprising story with a payoff, heated argument, emotional confession, useful insight. |
| `hype` | Clutch play, big win or loss, rage, screaming reaction. Loudness spikes plus chat bursts. |

- 9–10: instantly gripping in the first 2 s, clear payoff, rewatchable.
- 7–8: strong, but needs a beat of setup or has a softer payoff.
- ≤6: inside joke, needs long context, or no clear payoff. Skip.

Hard skips: dead air, ads/sponsor reads, "be right back" screens, anything that needs the previous 5 minutes to make sense, slurs, or sexual content.

Write all candidates to `sources/<slug>/edit/candidates.json`.

### 4. Select
Merge the candidate lists. Where two overlap, keep the higher score. Drop anything below the minimum, sort by score, and cap at the maximum.

### 5. Tighten each clip
Run `words.py` around each clip (±10 s) to get exact word times.
- **Start on the hook.** The first line should grab within 2 s. Cut setup down to under 3 s, or open on the reaction and let the setup follow.
- **End after the payoff plus the reaction.** The laugh is the beat. Don't end mid-laugh or on a dangling "so...".
- Start = first word's start − 0.05 s; end = last word's end + 0.15 s. Never cut inside a word.
- **Jump cuts:** if a pause of 1 s or more (marked `<-- gap`) sits inside the clip with nothing visual happening, split it into multiple ranges around the gap. Keep 0.1–0.2 s of air at each side.
- Stay within the length setting.

### 6. Pick the layout (once per source, re-check when the scene changes)
Grab 2–3 frames from different times of the source (`ffmpeg -ss <t> -i <video> -frames:v 1 -vf scale=960:-2 edit/verify/layout_<t>.png`) and Read them.

- **Facecam over gameplay** → `{"type": "split", "cam": [x, y, w, h], "game": [x, y, w, h], "cam_height": 768}`. Boxes are fractions 0–1 of the frame. Measure the cam box tightly from the frame (exclude borders and overlays). `game` is the region that matters, usually the centre ~60–75% of the width at full height. Use `cam_height` 640–860 depending on how important the face is.
- **Talking head / IRL / podcast, one main person** → `{"type": "center", "cx": <0–1 horizontal centre of the speaker's face>}`.
- **Two people side by side, wide shots, or anything a 9:16 crop would ruin** → `{"type": "blur"}`.
- **Already vertical source** → `{"type": "fit"}`.

If the facecam moves or disappears in some clips, give those clips their own layout.

### 7. Write clip specs
For clip NN (01 = highest score), write `sources/<slug>/edit/clips/NN.json`:

```json
{
  "source": "<info.video>",
  "edit_dir": "<abs>/sources/<slug>/edit",
  "ranges": [{"start": 812.35, "end": 829.10}, {"start": 830.40, "end": 838.25}],
  "layout": {"type": "split", "cam": [0.72, 0.05, 0.26, 0.30], "game": [0.15, 0, 0.7, 1], "cam_height": 768},
  "captions": true,
  "output": "<abs>/shorts/<slug>/NN_<category>_<title-slug>.mp4",
  "meta": {
    "title": "<= 70 chars, curiosity-driven, no clickbait lies, no ALL CAPS>",
    "description": "<1-2 sentences>\n\nCredit: <uploader> — <info.url>\n\n#shorts #<topic> #<creator>",
    "hashtags": ["#shorts", "..."],
    "category": "funny",
    "score": 9,
    "hook": "<opening line>",
    "creator": "<info.uploader>",
    "source_url": "<info.url>",
    "source_timestamps": "0:13:32-0:13:58"
  }
}
```

Add `"grade": "neutral_punch"` only if frames look flat or dark.

### 8. Render
`render_short.py sources/<slug>/edit/clips/*.json --jobs 3`. Each line reports `[OK ]`, `[BAD]` (wrong size or duration) or `[ERR]`. Fix BAD/ERR specs and re-render just those.

### 9. Self-check (no user stops)
Run `$PY clipper/sheet.py shorts/<slug>`. It writes `shorts/<slug>/_sheet.png`, one numbered frame per Short at 1.0 s (`--at 3` for another moment). Read the sheet and check:
- the face is fully in frame
- the split cam box isn't showing gameplay or borders
- captions are readable and not covering the face

Fix and re-render at most 2 times. Anything still wrong gets flagged in `index.md`.

### 10. Index + report
Write `shorts/<slug>/index.md`: source title, URL, then a table ranked by score with file, category, score, duration, title, source timestamps and flags.

Reply to the user with:
- how many Shorts were made, from what
- the folder path
- the top 3 titles
- Scribe hours used
- anything flagged

Keep it short.

## Notes
- Only clip content the user owns or has permission to clip (their own channel, or a creator's clipping program). If a URL is clearly someone else's content and the user hasn't said they have permission, mention it once in the final report; don't refuse.
- `shorts/<slug>/*.json` metadata is the input for the planned phase 2 (scheduled YouTube uploads). Keep its fields stable.
- Folder input: everything in `inbox/` (or the given folder) is processed. Sources that already have `shorts/<slug>/index.md` are skipped unless the user asks to redo them.
