---
name: shorts-clipper
description: Turn a YouTube/Twitch URL or a folder of videos into finished 9:16 YouTube Shorts of the funniest, most romantic and most engaging moments, saved to StreamVault/shorts/. Use whenever the user pastes a youtube.com, youtu.be or twitch.tv link, drops videos in inbox/, or asks to clip, find highlights, or make Shorts from a video or stream.
---

# Shorts clipper

Fully automatic: URL or folder in, finished Shorts out. **Do not stop to ask for approval**: the user chose hands-off mode, which overrides video-use's "confirm strategy first" rule for this workflow. Stop only for hard failures you cannot fix: a download that needs login cookies, a Scribe 401, VoiceStudio not running, or silent audio.

Built on video-use (`video-use/SKILL.md`). Its production rules still apply: cut on word boundaries, pad edges, subtitles last, never re-transcribe.

## Settings

| Setting | Value |
|---|---|
| Minimum score to render | **9** / 10 |
| Max Shorts per source | **3** |
| Clip length | **20–45 s** (cold open included) |
| Parallel renders | 3 |

The user's `style.json` in the repo root (written by the app's **Style** tab) holds their own values for these (`clip`) and free-text `instructions`. The app's runs spell both out in the prompt as "User settings for this run". The user can also override anything in their message ("only 8s and up", "max 5", "yellow captions").

**The look is your call, from the instructions.** There are no style settings: the user describes what they want in plain words, and you turn that into each clip's spec (`hook_text`, `cold_open`, `music_mood`, `emphasis`, `zooms`, `sfx`, `voice_hook`, and `style` for anything visual). Where the instructions say nothing, the defaults apply (see **Style keys**). "Always …" instructions apply to every clip; "on funny clips …" only to those.

## Quality bar
Quality over quantity: every Short goes out under the user's name.
- **It works cold.** A stranger who has never heard of the creator gets it with no context.
- **The payoff is inside the window.** The laugh, the reveal or the reaction happens in the clip, not after it.
- **No dead air.** No pause, setup or "so anyway" longer than 2 s. Cut it with jump cuts or drop the clip.
- **Zero Shorts from a weak video is a correct result.** Never lower the bar to fill the maximum. If nothing scores at the minimum, write `index.md` saying so and render nothing.

## Commands

Run everything from the StreamVault repo root (the folder holding this `.claude/`) with the video-use environment:

```bash
PY="uv run --project video-use python"
$PY clipper/fetch.py <url | file | folder> [--section 1:00:00-2:30:00] [--cookies-from-browser chrome]
$PY clipper/transcribe_long.py sources/<slug> [--engine local|scribe] [--language en]
$PY video-use/helpers/pack_transcripts.py --edit-dir sources/<slug>/edit
$PY clipper/signals.py sources/<slug>
$PY clipper/words.py sources/<slug> <start> <end>
$PY video-use/helpers/timeline_view.py <video> <start> <end> -o edit/verify/cand_<start>.png   # filmstrip + waveform + words
$PY clipper/track.py <video> <start> <end> --layout '<layout json>' --png edit/verify/track_<start>.png
$PY clipper/render_short.py sources/<slug>/edit/clips/*.json --jobs 3
$PY clipper/review.py shorts/<slug>
```

## Pipeline (per source)

### 1. Ingest
- URL: `fetch.py <url>`. A folder (default `inbox/`) or file: `fetch.py <path>`. Each source becomes `sources/<slug>/` with `info.json` (title, uploader, url, `video` path). Use the printed `SOURCE` lines.
- The user may give a time range ("only the first 2 hours"); pass it as `--section`.
- Download fails because the video is subscriber-only, members-only or age-gated: tell the user to rerun with `--cookies-from-browser chrome` (or their browser). Don't retry blindly.

### 2. Transcribe + signals
Run `transcribe_long.py`, then `pack_transcripts.py`, then `signals.py`. Transcripts are cached; never delete them. Long VODs are chunked automatically.

**Engine.** `--engine local` (the default, or whatever `CLIPPER_ASR` says) runs WhisperX on the GPU through VoiceStudio at `127.0.0.1:3900`: free, but no `(laughter)`/`(applause)` events, no speaker IDs, and fewer "um"s. `--engine scribe` uses ElevenLabs Scribe, which bills per audio hour. Use Scribe when the user asks for it, or when laughter detection matters more than cost. If VoiceStudio isn't answering, tell the user to start it; don't silently fall back to the paid engine. Report the engine and the audio hours transcribed in the final report (and the speed for local).

### 3. Find candidate moments
Inputs: `edit/takes_packed.md` (phrases with `[start-end]` seconds) and `edit/signals.md` (loudness spikes, Scribe audio events, chat bursts).

- Source **≤ 90 min**: read both yourself.
- Source **> 90 min**: spawn one sub-agent per 60-min window, **all in parallel in a single message**, with windows overlapping by 2 min. Each brief is self-contained:
  > "Read `<abs path>/takes_packed.md`, lines whose `[start-...]` falls in `<a>`–`<b>` s, plus the `<a>`–`<b>` entries in `<abs path>/signals.md`. Using the rubric below, return ONLY a JSON array of candidates `{start, end, category, hook, score, reason}`. Do not render, do not ask questions." Paste the rubric, the quality bar and the score guidance into the brief, and add: "Be strict: a 9 works cold for a stranger, has its payoff inside the clip and no dead air over 2 s. Returning no candidates is fine."

Signals point at moments; the transcript says what happens there. A loudness spike or chat burst with no transcript explanation may be a gameplay moment: check it with one frame (`ffmpeg -ss <t> -i <video> -frames:v 1 -vf scale=960:-2 edit/verify/t<t>.png`, then Read it) before scoring it.

**Rubric** (score 1–10 for how well the clip works on its own, with no context, to a stranger scrolling Shorts; apply the quality bar above):

| Category | What it looks like |
|---|---|
| `funny` | Punchline, roast, absurd line, fail, someone breaking down laughing. `(laughter)` events and laugh-heavy chat bursts are strong evidence. |
| `romantic` | Flirting, compliments, blushing, a sweet or tender exchange, a couple teasing each other, "rizz" moments. |
| `engaging` | Hot take, surprising story with a payoff, heated argument, emotional confession, useful insight. |
| `hype` | Clutch play, big win or loss, rage, screaming reaction. Loudness spikes plus chat bursts. |

- 9–10: instantly gripping in the first 2 s, clear payoff inside the clip, no dead air, rewatchable. Rare: most long videos have 0–3.
- 7–8: strong, but needs a beat of setup or has a softer payoff. Below the default bar.
- ≤6: inside joke, needs long context, or no clear payoff. Skip.

Score honestly and low: a 9 is a clip you would send a friend. Anything with setup over 2 s, a payoff that lands after the window, or that needs context is 8 at most.

Hard skips: dead air, ads/sponsor reads, "be right back" screens, anything that needs the previous 5 minutes to make sense, slurs, or sexual content.

**Watch the best ones.** The transcript only tells you what was said. For the top ~8 candidates (the ones that could reach the minimum), run `timeline_view.py` on the candidate window and Read the PNG: a filmstrip of frames with the waveform and words. Judge what a viewer *sees*: is there a visible reaction or action at the payoff (a face breaking, a jump, a fail)? Is the subject on screen and facing the camera, or turned away, off-frame, behind a menu? Raise the score for a strong visual payoff, lower it (or skip) when the moment is invisible or the subject is out of shot.

Write all candidates to `sources/<slug>/edit/candidates.json`.

### 4. Select
Merge the candidate lists. Where two overlap, keep the higher score. Drop anything below the minimum, sort by score, and cap at the maximum.

### 5. Tighten each clip, then write the hook
Run `words.py` around each clip (±10 s) to get exact word times.
- **Start on the hook.** The first line should grab within 2 s. Cut setup down to under 3 s, or open on the reaction and let the setup follow.
- **End after the payoff plus the reaction.** The laugh is the beat. Don't end mid-laugh or on a dangling "so...".
- Start = first word's start − 0.05 s; end = last word's end + 0.15 s. Never cut inside a word.
- **Jump cuts:** if a pause of 1 s or more (marked `<-- gap`) sits inside the clip with nothing visual happening, split it into multiple ranges around the gap. Keep 0.1–0.2 s of air at each side.
- Stay within the length setting (`clip.minSeconds`–`clip.maxSeconds`, cold open included); the renderer flags anything outside it as BAD.

**Hook** (the first ~3 s decide whether anyone watches):
- `hook_text`: ≤ 8 words of curiosity or tension that make a stranger stay for the payoff. Not the punchline, not the title. Good: "SHE SWORE SHE WATCHES HIM EVERY NIGHT", "HE DIDN'T KNOW THE MIC WAS ON". Skip it when the hook banner is off.
- `cold_open`: a flash-forward of the payoff (`{start, end}` in source seconds, about `hook.coldOpenSeconds` long, usually the reaction or the punchline), played first under the hook text before the clip starts from the top. With `coldOpen: auto` set it only when there is a clear payoff or reaction that hooks on its own; leave it out otherwise. With `always`, always set one; with `off`, never.
- `music_mood`: `funny`, `hype`, `romantic` or `chill`, matching the clip.
- `emphasis`: 1–3 words per caption-worth of speech to colour, when the caption highlight is set to keywords: the punch words (names, numbers, the reaction word), never filler.
- `style`: whatever the user's instructions change about the look, for this clip (e.g. "yellow captions on funny clips" → `{"captions": {"highlight": "#FFE14D"}}`). Keys in **Style keys** below; colours are `#RRGGBB`.

**Retention** (a visual or audio change every 2–3 s keeps people watching):
- `zooms`: `[{"at": <source s>, "dur": 0.6, "scale": 1.15}]`, a quick punch-in on the payoff, the reaction face and the punch words; 1–4 per clip, never on dead air. The cold open's start gets one automatically.
- `sfx`: `[{"at": <source s>, "sound": "impact/boom"}]`, tied to something visible: a boom on the punchline, a gasp or laugh on a reaction, a record scratch on an awkward stop, a ding on a correct answer. `sound` is a file in `sfx/` (`sfx/library.json` lists them with tags) or `category/tag` (`transition/whoosh`, `impact/boom`, `reaction/laugh`, `reaction/gasp`, `ui/pop`) or a category. A whoosh on each cut and a riser into the payoff are added automatically. 1–3 of your own per clip: fewer, well-placed effects beat a wall of noise.
- `voice_hook`: a narrator line spoken over the cold open (VoiceStudio, a stock voice), or `true` to speak `hook_text`. Use it when the instructions ask for a voice, or when the cold open has little speech of its own and the hook needs saying; not when the cold open is someone talking.
- **Loop ending:** end on a line or beat that leads back into the cold open (the question the payoff answers, a reaction that matches the first frame), so the replay feels continuous. Never end on a call to action or "follow for more".

### 6. Frame each clip: keep the subject in view
Grab 2–3 frames from different times of the source (`ffmpeg -ss <t> -i <video> -frames:v 1 -vf scale=960:-2 edit/verify/layout_<t>.png`) and Read them to see what kind of video it is. Then, **for every clip**, run `track.py` on its range with the layout you have in mind and Read the PNG: green is what the Short shows, red is the face it follows. Its JSON line gives `coverage` (share of frames with the face inside the Short) and `detected` (share with any face).

- **One person who moves (IRL, walking, standing up)** → `{"type": "track"}`: the crop follows their face smoothly. The default for IRL streams.
- **Talking head who stays put / podcast seat** → `{"type": "center", "cx": <0–1 horizontal centre of the face>}`, or `track`.
- **Facecam over gameplay** → `{"type": "split", "cam": [x, y, w, h], "game": [x, y, w, h], "cam_height": 768}`. Boxes are fractions 0–1 of the frame. Measure the cam box tightly from the frame (exclude borders and overlays). `game` is the region that matters, usually the centre ~60–75% of the width at full height. Use `cam_height` 640–860 depending on how important the face is.
- **Two people both mattering, far apart, or wide shots a 9:16 crop would ruin** → `{"type": "blur"}`.
- **Already vertical source** → `{"type": "fit"}`.

Coverage under 0.8 with faces detected means the subject leaves the Short: try `track`, re-measure the cam box, move `cx`, or switch to `blur`. The renderer checks this too and reports `[BAD] subject out of frame`. If the facecam moves or disappears in some clips, give those clips their own layout. If the face sits where the captions go, move them with `style.captions.position`.

### 7. Write clip specs
For clip NN (01 = highest score), write `sources/<slug>/edit/clips/NN.json`:

```json
{
  "source": "<info.video>",
  "edit_dir": "<abs>/sources/<slug>/edit",
  "ranges": [{"start": 812.35, "end": 829.10}, {"start": 830.40, "end": 838.25}],
  "cold_open": {"start": 835.90, "end": 838.25},
  "hook_text": "HE DID NOT SEE THAT COMING",
  "emphasis": ["never", "million"],
  "music_mood": "funny",
  "zooms": [{"at": 836.4, "dur": 0.6, "scale": 1.15}],
  "sfx": [{"at": 836.4, "sound": "impact/boom"}],
  "voice_hook": true,
  "style": {},
  "layout": {"type": "track"},
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

Add `"grade": "neutral_punch"` only if frames look flat or dark. Leave out `cold_open`, `hook_text`, `emphasis`, `zooms`, `sfx`, `voice_hook` or `style` when they don't apply.

### 8. Render
`render_short.py sources/<slug>/edit/clips/*.json --jobs 3`. Each line reports `[OK ]`, `[BAD]` (wrong size, duration or length) or `[ERR]`. Fix BAD/ERR specs and re-render just those. `[BAD] subject out of frame` means fix the layout (step 6). Notes like "no music", "no sound effects" or "voice hook skipped" mean the library is empty or VoiceStudio isn't answering: mention them in the report, they're not errors.

### 9. Self-check (no user stops)
Run `$PY clipper/review.py shorts/<slug>`. It writes `shorts/<slug>/_review_NN.png` per Short: the first look at 0.3 s, the end of the hook, both sides of each cut, the payoff, and the last frame next to the first (the loop seam). Read every one and check:
- the subject is in frame in every shot (the whole face, not cut by the edge)
- the split cam box isn't showing gameplay or borders
- captions and the hook banner are readable and not covering the face
- the first look grabs on its own, and the zoom lands on the reaction, not on dead air
- the loop seam: the last frame flows into the first

Fix and re-render at most 2 times. Anything still wrong gets flagged in `index.md`.

### 10. Index + report
Write `shorts/<slug>/index.md`: source title, URL, then a table ranked by score with file, category, score, duration, title, source timestamps and flags.

Reply to the user with:
- how many Shorts were made, from what
- the folder path
- the top 3 titles
- transcription engine and audio hours (plus minutes per audio hour for local)
- anything flagged

Keep it short.

## Style keys
What `spec.style` can change (defaults in `clipper/style_defaults.json`; only include what differs). Colours are `#RRGGBB`; sizes are px on the 1080x1920 frame.
- `hook`: `text` (show hook_text), `seconds` (on screen, default 3.5), `font`, `size` (62), `color` (#4CFF4C), `glow`, `box` + `boxOpacity` (0–100; 0 = no box), `position` (top | middle | bottom), `animation` (type | fade | none), `coldOpen` (auto | always | off), `coldOpenSeconds` (3), `flash` (white flash on the cut out of the cold open), `loop` (true), `voice` (allow voice_hook), `voicePreset` (a VoiceStudio voice: narrator, casual, news_anchor, storyteller, energetic, gravelly_villain, aussie_podcaster, bedtime_storyteller, or a description like "young adult male, low pitch"), `voiceVolume` (1.0)
- `captions`: `enabled`, `font` (Arial Black; any installed font, e.g. Impact, Bahnschrift), `size` (70), `color` (#FFFFFF), `outline`, `outlineWidth` (3), `shadow` (4), `highlight` (#4CFF4C), `highlightMode` (keywords: your `emphasis` words | active: the word being spoken | off), `position` (% of the height from the bottom, 42), `uppercase`, `wordsPerCaption` (1–3), `reveal` (words appear as spoken)
- `music`: `enabled`, `volume` (0.4: vs the voice in pauses; it ducks under speech), `duck`
- `zoom`: `enabled`, `scale` (1.15), `auto` (punch in at the cold open's start)
- `sfx`: `enabled`, `volume` (0.8), `autoCuts` (whoosh on cuts, riser into the payoff)
- `framing`: `track` (follow the face in `track` layouts)

Plain words to keys, for example: "yellow captions" → `captions.highlight: "#FFE14D"` (or `color` for all words); "bigger captions" → `captions.size: 84`; "2 words at a time" → `captions.wordsPerCaption: 2`; "no music" → `music.enabled: false`; "quieter music" → `music.volume: 0.25`; "no zooms" → `zoom.enabled: false`; "boxed hook at the top" → `hook.position: "top", hook.boxOpacity: 70`; "deep voice" → `hook.voicePreset: "gravelly_villain"`; "no cold open" → `hook.coldOpen: "off"`.

## Posting to YouTube (only when the user asks)
Not part of the automatic run: publishing is outward-facing, so it waits for the user to ask.

Sources added in the StreamVault desktop app (`app/`, engine `clipper/run_queue.py`, see `QUEUE_SETUP.md`) are exported to `outbox/`, uploaded by the app to online OneDrive `StreamVault/ready/`, and posted by n8n every 4 hours: don't also upload those by hand. The app's runs arrive with the transcript, `takes_packed.md` and `signals.md` already made, so they start at step 3. Use `clipper/export_posts.py shorts/<slug> --priority high|normal|low` to hand a manually clipped source to the same poster (the app uploads it while it's running).

1. Write `shorts/<slug>/posts_copy.json` (format in `clipper/plan_posts.py`'s docstring): per Short a title (≤ 100 chars, creator name near the front), the hook as a quote, a one-sentence setup, 5 topical tags and 2 extra hashtags. The description gets a credit line with the source URL, so only use "with permission" if the user said they have it.
2. `$PY clipper/plan_posts.py shorts/<slug>` assigns hourly US Eastern slots (best-scored Shorts at 12, 13, 18–20 h), writes a `youtube` block into each meta JSON and `shorts/<slug>/schedule.md`. Shorts already published or scheduled are skipped.
3. Upload in YouTube Studio through Claude in Chrome, in slot order. The Chrome upload tool can't send files over 10 MB, so the user picks each file in Studio's **Select files** dialog; Claude fills in title, description, "not made for kids", tags, language English, category Entertainment, AI use "No", then Visibility → Schedule at the slot time. If Checks reports a copyright claim, stop and tell the user.
   - The user can pick several files at once; Studio makes one draft each, opened from the uploads panel's pencil icons.
   - Never press Ctrl+A in an empty description box: it types an "a". Select-all only fields that already have text.
   - After Publish/Schedule, a "Video published" popup appears a few seconds later; close it before opening the next draft.
   - An unverified channel can upload 10 videos a day; the rest fail with "Daily upload limit reached". Tell the user to verify by phone at https://www.youtube.com/verify, then re-upload those.
4. After each one, set `studio_status` to `scheduled` (or `published`) with `video_id`/`url` in the meta JSON and the row in `schedule.md`.

The YouTube Data API isn't used: uploads from an unaudited API project are locked to private.

## Notes
- Only clip content the user owns or has permission to clip (their own channel, or a creator's clipping program). If a URL is clearly someone else's content and the user hasn't said they have permission, mention it once in the final report; don't refuse.
- Background music comes from the user's `music/` folder (their own cleared tracks, or ones generated with their ElevenLabs account), sound effects from `sfx/`. Both have a `library.json` recording each file's source. Never download or add songs yourself: a copyrighted song gets a Content ID claim.
- `shorts/<slug>/*.json` metadata feeds `export_posts.py` and `plan_posts.py`. Keep its fields stable. `"flagged": "<reason>"` holds a Short back from the queue.
- Folder input: everything in `inbox/` (or the given folder) is processed. Sources that already have `shorts/<slug>/index.md` are skipped unless the user asks to redo them.
