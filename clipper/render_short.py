"""Render one or more 9:16 YouTube Shorts from clip specs.

Composes video-use's render.py functions unmodified:
  extract_segment per range (30ms fades), each with its own filter: grade → layout (the `track`
    layout follows the subject's face, see track.py) → punch-in zooms
  → concat_segments (lossless)
  → our own ASS file (captions, hook banner) from cached transcript words, chunked by
    render._words_in_range + render.chunk_words
  → one composite pass: subtitles=<file>.ass LAST; audio = the clip (ducked under a spoken voice hook)
    + sound effects + background music ducked under the voice (replaces render.build_final_composite)
  → apply_loudnorm_two_pass (-14 LUFS / -1 dBTP)

The look comes from style.py: DEFAULTS < <workspace>/style.json (or --style FILE) < spec["style"];
Claude writes spec["style"] from the user's plain-language instructions.
Each clip renders in its own work dir, so several can run in parallel.

Clip spec (JSON):
{
  "source":   "C:/.../sources/<slug>/video.mp4",
  "edit_dir": "C:/.../sources/<slug>/edit",          # holds transcripts/<source stem>.json
  "ranges":   [{"start": 812.40, "end": 838.10}, ...],  # source seconds; >1 range = jump cuts
  "cold_open": {"start": 836.2, "end": 838.1},       # optional: payoff played first, source seconds
  "hook_text": "HE DID NOT SEE THAT COMING",           # optional: banner over the first seconds
  "emphasis": ["never", "million"],                   # optional: words to colour (highlightMode keywords)
  "music_mood": "funny",                              # optional: funny | hype | romantic | chill
  "zooms":    [{"at": 836.4, "dur": 0.6, "scale": 1.15}],  # optional punch-ins, source seconds
  "sfx":      [{"at": 836.4, "sound": "impact/boom"}],   # optional: a file in sfx/, category/tag, or tag
  "voice_hook": "He had no idea" | true,              # optional: spoken over the cold open (true = hook_text)
  "style":    {"captions": {"highlight": "#FF4D4D"}}, # optional: per-clip overrides of style.json
  "layout":   {"type": "track"}                          # crop follows the main face (OpenCV)
            | {"type": "split", "cam": [x, y, w, h], "game": [x, y, w, h], "cam_height": 768}
            | {"type": "center", "cx": 0.5}             # cx = speaker centre, fraction of width
            | {"type": "blur"}
            | {"type": "fit"},                          # already-vertical sources
  "grade":    "neutral_punch",                         # optional video-use preset or raw filter
  "captions": true,                                    # default true
  "output":   "C:/.../shorts/<slug>/01_funny_title.mp4",
  "meta":     {...}                                    # optional; written next to output as .json
}
Boxes are fractions (0..1) of the source frame, so they hold for any resolution.

Music comes from <workspace>/music/<mood>/ (or music/ itself), sound effects from <workspace>/sfx/
(see sounds.py): the user's cleared or ElevenLabs-generated sounds only, since a copyrighted song gets a
Content ID claim. Missing sounds, a missing VoiceStudio or missing OpenCV mean a note, not a failure.
For `center`, `track` and `split`, a subject the Short doesn't show (under 80% of the frames) is BAD.

Usage:
    python clipper/render_short.py clip.json [clip2.json ...] [--jobs 3] [--draft] [--style style.json]
"""

from __future__ import annotations

import _env  # noqa: F401  (must come first: sys.path, PATH, UTF-8)

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import render
import requests
import style as styles
import track as tracker
from grade import get_preset

OUT_W, OUT_H = 1080, 1920
SIDE_MARGIN = 60
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}
HOOK_TOP_MARGIN = 330                       # px below the top edge, clear of the Shorts top bar
HOOK_BOTTOM_FRACTION = 0.30                 # "bottom" still sits above the Shorts UI
TYPE_SECONDS = 1.2                          # typewriter reveal of the hook
FLASH_SECONDS = 0.2

# render.chunk_words reads module constants; parallel renders may want different values.
CHUNK_LOCK = threading.Lock()


def _fill(w: int, h: int) -> str:
    """Scale to cover w×h, then centre-crop to exactly w×h."""
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"


def _crop_box(box: list[float]) -> str:
    x, y, w, h = box
    return f"crop=w=iw*{w}:h=ih*{h}:x=iw*{x}:y=ih*{y}"


def layout_filter(layout: dict, x: str | None = None) -> str:
    """The 9:16 framing. `x` overrides the centre crop's x expression (track.x_expr: follow the face)."""
    kind = layout.get("type", "blur")
    if kind in ("center", "track"):
        cx = float(layout.get("cx", 0.5))
        x = x or f"max(0,min(iw-ow,{cx}*iw-ow/2))"
        return f"crop=w=ih*9/16:h=ih:x='{x}':y=0,scale={OUT_W}:{OUT_H}"
    if kind == "split":
        cam_h = int(layout.get("cam_height", 768))
        game_h = OUT_H - cam_h
        game = layout.get("game", [0, 0, 1, 1])
        return (
            "split[a][b];"
            f"[a]{_crop_box(layout['cam'])},{_fill(OUT_W, cam_h)}[cam];"
            f"[b]{_crop_box(game)},{_fill(OUT_W, game_h)}[game];"
            "[cam][game]vstack"
        )
    if kind == "fit":
        return (
            f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
            f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2"
        )
    if kind == "blur":
        return (
            "split[a][b];"
            f"[a]{_fill(OUT_W, OUT_H)},boxblur=20:2[bg];"
            f"[b]scale={OUT_W}:-2[fg];"
            "[bg][fg]overlay=0:(H-h)/2"
        )
    raise ValueError(f"unknown layout type: {kind}")


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(out.stdout)
    s = data["streams"][0]
    return {"width": s["width"], "height": s["height"], "duration": float(data["format"]["duration"])}


# ---------------------------------------------------------------- ranges

def edit_ranges(spec: dict, st: dict) -> tuple[list[dict], float]:
    """The ranges to cut, cold open first. Returns (ranges, cold open length in seconds)."""
    ranges = [{"start": float(r["start"]), "end": float(r["end"])} for r in spec["ranges"]]
    mode = st["hook"]["coldOpen"]
    co = spec.get("cold_open")
    if mode == "off":
        co = None
    elif mode == "always" and not co and ranges:
        # Claude didn't pick one: the payoff is usually the clip's last seconds.
        last = ranges[-1]
        co = {"start": max(last["start"], last["end"] - st["hook"]["coldOpenSeconds"]), "end": last["end"]}
    if co and float(co["end"]) > float(co["start"]):
        cold = {"start": float(co["start"]), "end": float(co["end"])}
        return [cold, *ranges], cold["end"] - cold["start"]
    return ranges, 0.0


# ---------------------------------------------------------------- ASS subtitles

def ass_rgb(hex_rgb: str) -> str:
    """'#RRGGBB' -> '&HBBGGRR&' for inline \\c tags."""
    return styles.ass_color(hex_rgb)[:2] + styles.ass_color(hex_rgb)[4:] + "&"


def ass_time(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "\u29f5").replace("{", "(").replace("}", ")").replace("\n", " ")


def _bold(font: str) -> int:
    # Single-weight heavy fonts look smeared when libass fakes a bold on top.
    return 0 if re.search(r"black|heavy|impact", font, re.I) else 1


def hook_margin(st: dict) -> tuple[int, int]:
    """(Alignment, MarginV) of the hook line."""
    pos = st["hook"]["position"]
    if pos == "top":
        return 8, HOOK_TOP_MARGIN
    if pos == "bottom":
        return 2, round(OUT_H * HOOK_BOTTOM_FRACTION)
    return 2, caption_margin(st)


def caption_margin(st: dict) -> int:
    return round(OUT_H * st["captions"]["position"] / 100)


def ass_header(st: dict) -> str:
    c, h = st["captions"], st["hook"]
    cap = (f"Style: Caption,{c['font']},{c['size']},{styles.ass_color(c['color'])},&H000000FF,"
           f"{styles.ass_color(c['outline'])},&H80000000,{_bold(c['font'])},0,0,0,100,100,0,0,1,"
           f"{c['outlineWidth']},{c['shadow']},2,{SIDE_MARGIN},{SIDE_MARGIN},{caption_margin(st)},1")
    align, margin = hook_margin(st)
    if h["boxOpacity"] > 0:   # BorderStyle 3: an opaque box in OutlineColour, Outline = padding
        border = f"{styles.ass_color(h['box'], h['boxOpacity'])},&H00000000,{_bold(h['font'])},0,0,0,100,100,0,0,3,18,0"
    else:                     # plain text: thin dark edge and a drop shadow
        border = f"&H00101010,&H60000000,{_bold(h['font'])},0,0,0,100,100,0,0,1,2,4"
    hook = (f"Style: Hook,{h['font']},{h['size']},{styles.ass_color(h['color'])},&H000000FF,{border},"
            f"{align},{SIDE_MARGIN},{SIDE_MARGIN},{margin},1")
    flash = "Style: Flash,Arial,10,&H00FFFFFF,&H000000FF,&H00FFFFFF,&H00FFFFFF,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1"
    return "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {OUT_W}", f"PlayResY: {OUT_H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        cap, hook, flash, "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ])


def dialogue(layer: int, start: float, end: float, style_name: str, text: str) -> str:
    return f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style_name},,0,0,0,,{text}"


def hook_events(text: str, st: dict, video_end: float) -> list[str]:
    """The hook banner from 0 to hook.seconds: typed on, faded or plain, with an optional glow."""
    h = st["hook"]
    if not h["text"] or not text.strip():
        return []
    text = re.sub(r"\s+", " ", text.strip())
    if st["captions"]["uppercase"]:
        text = text.upper()
    end = min(h["seconds"], video_end)
    glow = h["glow"] and h["boxOpacity"] == 0
    glow_tags = (f"\\1a&HFF&\\3c{ass_rgb(h['color'])}\\3a&H50&\\bord{max(6, h['size'] // 6)}"
                 f"\\blur{max(8, h['size'] // 5)}\\shad0")

    def line(shown: str, hidden: str, t0: float, t1: float, tags: str) -> list[str]:
        body = ass_escape(shown) + (f"{{\\alpha&HFF&}}{ass_escape(hidden)}" if hidden else "")
        out = [dialogue(2, t0, t1, "Hook", f"{{{tags}}}{body}")]
        if glow:
            out.insert(0, dialogue(1, t0, t1, "Hook", f"{{{tags}{glow_tags}}}{body}"))
        return out

    events: list[str] = []
    if h["animation"] == "type":
        # One event per typed character (a space alone shows nothing new); the untyped rest is
        # drawn invisible so the line keeps its final, centred position as it grows.
        typing = min(TYPE_SECONDS, end * 0.4)
        cuts = [i for i in range(1, len(text) + 1) if text[i - 1] != " "]
        for k, i in enumerate(cuts):
            t0 = typing * k / len(cuts)
            t1 = typing * (k + 1) / len(cuts) if i < len(text) else end
            events += line(text[:i], text[i:], t0, t1, "\\fad(0,250)" if i == len(text) else "")
    elif h["animation"] == "fade":
        events += line(text, "", 0, end, "\\fad(200,250)")
    else:
        events += line(text, "", 0, end, "")
    return events


def caption_events(edl: dict, edit_dir: Path, st: dict, emphasis: list[str], skip_before: float,
                   until: float = float("inf")) -> list[str]:
    """Output-timeline captions: chunked words, optionally revealed word by word and highlighted."""
    c = st["captions"]
    n = c["wordsPerCaption"]
    keywords = {re.sub(r"[^\w']", "", w).lower() for w in emphasis or [] if isinstance(w, str)}
    mode = c["highlightMode"]
    per_word = c["reveal"] or mode == "active"
    base, hl = ass_rgb(c["color"]), ass_rgb(c["highlight"])

    cues: list[tuple[list[str], list[float], float, list[bool]]] = []   # (texts, word starts, end, keyword?)
    seg_offset = 0.0
    for r in edl["ranges"]:
        seg_start, seg_end = float(r["start"]), float(r["end"])
        tr_path = edit_dir / "transcripts" / f"{r['source']}.json"
        if not tr_path.exists():
            print(f"  no transcript for {r['source']}, no captions for this segment")
            seg_offset += seg_end - seg_start
            continue
        transcript = json.loads(tr_path.read_text(encoding="utf-8"))
        words = render._words_in_range(transcript, seg_start, seg_end)
        with CHUNK_LOCK:
            saved = render.CHUNK_WORDS, render.CHUNK_MAX_WORDS
            render.CHUNK_WORDS, render.CHUNK_MAX_WORDS = n, n   # a cue holds up to n words
            try:
                chunks = render.chunk_words(words)
            finally:
                render.CHUNK_WORDS, render.CHUNK_MAX_WORDS = saved

        def out_t(t: float) -> float:
            return min(max(t, seg_start), seg_end) - seg_start + seg_offset

        for chunk in chunks:
            # Captions read cleaner without commas and full stops; ? and ! carry tone, so they stay.
            texts = [re.sub(r"\s+", " ", (w.get("text") or "").strip()).rstrip(",;:.") or "…" for w in chunk]
            if c["uppercase"]:
                texts = [t.upper() for t in texts]
            starts = [out_t(w["start"]) for w in chunk]
            c_end = min(max(out_t(chunk[-1]["end"]), starts[0] + 0.4), until)
            if starts[0] < skip_before or starts[0] >= until:
                continue
            is_kw = [mode == "keywords" and re.sub(r"[^\w']", "", t).lower() in keywords for t in texts]
            cues.append((texts, starts, c_end, is_kw))
        seg_offset += seg_end - seg_start

    # A cue runs until the next one starts at the latest: two at once get stacked by libass.
    events: list[str] = []
    for k, (texts, starts, c_end, is_kw) in enumerate(cues):
        if k + 1 < len(cues):
            c_end = min(c_end, cues[k + 1][1][0])

        def render_words(active: int | None) -> str:
            parts = []
            for j, t in enumerate(texts):
                lit = (mode == "active" and j == active) or is_kw[j]
                hidden = c["reveal"] and active is not None and j > active
                parts.append(f"{{\\1c{hl if lit else base}\\alpha&H{'FF' if hidden else '00'}&}}{ass_escape(t)}")
            return " ".join(parts)

        if per_word:
            for j in range(len(texts)):
                t0 = starts[0] if j == 0 else max(starts[j], starts[0])
                t1 = min(starts[j + 1], c_end) if j + 1 < len(texts) else c_end
                if t1 > t0:
                    events.append(dialogue(0, t0, t1, "Caption", render_words(j)))
        elif c_end > starts[0]:
            events.append(dialogue(0, starts[0], c_end, "Caption", render_words(None)))
    return events


def flash_event(at: float) -> str:
    box = f"m 0 0 l {OUT_W} 0 {OUT_W} {OUT_H} 0 {OUT_H}"
    return dialogue(9, at, at + FLASH_SECONDS, "Flash", f"{{\\an7\\pos(0,0)\\alpha&H30&\\fad(0,{int(FLASH_SECONDS * 1000)})\\p1}}{box}{{\\p0}}")


def hook_hides_captions(st: dict) -> bool:
    """The hook and the captions would overlap: keep captions off screen while the hook shows."""
    align, margin = hook_margin(st)
    if align != 2:
        return False
    return abs(margin - caption_margin(st)) < (st["hook"]["size"] + st["captions"]["size"]) * 1.3


def build_ass(edl: dict, edit_dir: Path, st: dict, spec: dict, duration: float, cold_len: float,
              captions: bool) -> str:
    hook = hook_events(spec.get("hook_text") or "", st, duration)
    skip = st["hook"]["seconds"] if hook and hook_hides_captions(st) else 0.0
    events = list(hook)
    if captions and st["captions"]["enabled"]:
        # Loop ending: the last moment stays clean so the replay's first frame reads as a cut, not a caption flash.
        until = duration - 0.25 if st["hook"]["loop"] else float("inf")
        events += caption_events(edl, edit_dir, st, spec.get("emphasis") or [], skip, until)
    if cold_len and st["hook"]["flash"]:
        events.append(flash_event(cold_len))
    return ass_header(st) + "\n" + "\n".join(events) + "\n"


# ---------------------------------------------------------------- music

def find_tracks(root: Path, st: dict, mood: str | None) -> tuple[list[Path], str]:
    """Tracks for this mood: music/<mood>/, else music/ itself, else any mood folder."""
    folder = Path(st["music"]["folder"])
    folder = folder if folder.is_absolute() else root / folder
    if not folder.is_dir():
        return [], f"{folder.name}/"

    def audio(d: Path) -> list[Path]:
        return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS) if d.is_dir() else []

    if mood in styles.MOODS and audio(folder / mood):
        return audio(folder / mood), f"{folder.name}/{mood}/"
    if audio(folder):
        return audio(folder), f"{folder.name}/"
    every = sorted(p for d in folder.iterdir() if d.is_dir() and not d.name.startswith("_") for p in audio(d))
    return every, f"{folder.name}/*/"


def pick_track(tracks: list[Path], out_path: Path) -> Path:
    """Deterministic per clip; consecutive clips of one source rotate through the tracks."""
    m = re.match(r"(\d+)_", out_path.name)
    nn = int(m.group(1)) if m else zlib.crc32(out_path.stem.encode())
    return tracks[(nn - 1 + zlib.crc32(out_path.parent.name.encode())) % len(tracks)]


def loudness(path: Path) -> float | None:
    m = render.measure_loudness(path)
    try:
        v = float(m["input_i"]) if m else None
    except (TypeError, ValueError):
        return None
    return v if v is not None and math.isfinite(v) and v > -70 else None


def plan_music(base: Path, st: dict, mood: str | None, out_path: Path) -> tuple[dict | None, str | None]:
    """({track, gain_db, duck, threshold} for audio_graph(), note for the result)."""
    m = st["music"]
    if not m["enabled"] or m["volume"] <= 0:
        return None, None
    tracks, where = find_tracks(_env.ROOT, st, mood)
    if not tracks:
        return None, f"no music: no tracks in {where}"
    track = pick_track(tracks, out_path)
    voice = loudness(base) or -20.0
    music = loudness(track) or -14.0
    # Key on anything within 14 dB of the voice's average: ratio 8 then pulls the music ~10-12 dB
    # under speech (what the reference Short does) and lets it back up in the pauses.
    return {"track": track, "gain_db": voice - music + 20 * math.log10(m["volume"]), "duck": m["duck"],
            "threshold": min(0.5, max(0.001, 10 ** ((voice - 14) / 20)))}, None


# ---------------------------------------------------------------- sound effects

def sfx_folder(root: Path, st: dict) -> Path:
    folder = Path(st["sfx"]["folder"])
    return folder if folder.is_absolute() else root / folder


def sfx_library(folder: Path) -> list[dict]:
    """sfx/library.json entries ({file, category, tags, hit_ms, lufs}); without it, the files on disk."""
    lib = folder / "library.json"
    entries = []
    if lib.exists():
        try:
            entries = [e for e in json.loads(lib.read_text(encoding="utf-8")).get("sounds", [])
                       if isinstance(e, dict) and e.get("file")]
        except (OSError, ValueError):
            entries = []
    known = {e["file"] for e in entries}
    if folder.is_dir():
        for p in sorted(folder.rglob("*")):
            rel = p.relative_to(folder).as_posix()
            if p.suffix.lower() in AUDIO_EXTS and rel not in known and not rel.startswith("_"):
                entries.append({"file": rel, "category": p.parent.name if p.parent != folder else "",
                                "tags": re.split(r"[_\-\s]+", re.sub(r"\d+$", "", p.stem.lower()).strip("_- ")),
                                "hit_ms": 0})
    return [e for e in entries if (folder / e["file"]).exists()]


def resolve_sfx(lib: list[dict], sound: str, key: int) -> dict | None:
    """"impact/boom_1.mp3" (a file), "impact/boom" (category/tag), "boom" (tag) or "impact" (category)."""
    sound = str(sound).strip().lower().replace("\\", "/")
    exact = [e for e in lib if e["file"].lower() == sound]
    if exact:
        return exact[0]
    cat, _, tag = sound.rpartition("/")

    def has_tag(e: dict, t: str) -> bool:
        return t in [x.lower() for x in e.get("tags", [])] or t in Path(e["file"]).stem.lower()

    hits = [e for e in lib if (not cat or e.get("category", "").lower() == cat) and has_tag(e, tag)]
    if not hits and not cat:
        hits = [e for e in lib if e.get("category", "").lower() == tag]
    return hits[key % len(hits)] if hits else None


def output_times(ranges: list[dict], src_t: float) -> list[float]:
    """Every output time at which source second `src_t` plays (the cold open repeats the payoff)."""
    out, offset = [], 0.0
    for r in ranges:
        if r["start"] <= src_t < r["end"]:
            out.append(offset + src_t - r["start"])
        offset += r["end"] - r["start"]
    return out


def plan_sfx(spec: dict, st: dict, ranges: list[dict], cold_len: float, out_path: Path) -> tuple[list[dict], list[str]]:
    """[{file, at (output s), gain_db}] from spec.sfx plus the automatic whoosh/riser, and notes."""
    s = st["sfx"]
    if not s["enabled"] or s["volume"] <= 0:
        return [], []
    folder = sfx_folder(_env.ROOT, st)
    lib = sfx_library(folder)
    wanted: list[tuple[str, float]] = []    # (sound, output seconds)
    for item in spec.get("sfx") or []:
        sound = item.get("sound", item.get("file")) if isinstance(item, dict) else None
        if sound is not None and item.get("at") is not None:
            for t in output_times(ranges, float(item["at"])):
                wanted.append((str(sound), t, float(item.get("gain_db", 0))))
    if s["autoCuts"]:
        cuts, offset = [], 0.0
        for r in ranges[:-1]:
            offset += r["end"] - r["start"]
            cuts.append(offset)
        last = -1e9
        for t in cuts:
            if (cold_len and abs(t - cold_len) < 1e-6) or t - last >= 4.0:
                wanted.append(("transition/whoosh", t, 0.0))
                last = t
        if cold_len and spec.get("cold_open"):
            later = [t for t in output_times(ranges, float(spec["cold_open"]["start"])) if t >= cold_len]
            if later:
                wanted.append(("transition/riser", later[0], -3.0))
    if not wanted:
        return [], []
    if not lib:
        return [], [f"no sound effects: {folder.name}/ is empty"]
    base_key = zlib.crc32(out_path.name.encode())
    plan, missing = [], set()
    for i, (sound, at, gain) in enumerate(wanted):
        e = resolve_sfx(lib, sound, base_key + i)
        if not e:
            missing.add(sound)
            continue
        lufs = e.get("lufs")
        level = (-20.0 - float(lufs)) if isinstance(lufs, (int, float)) else 0.0   # effects sit ~6 dB under -14 LUFS speech
        plan.append({"file": folder / e["file"], "at": at - float(e.get("hit_ms") or 0) / 1000,
                     "gain_db": level + gain + 20 * math.log10(s["volume"]), "sound": e["file"]})
    notes = [f"no sound effect for {', '.join(sorted(missing))}"] if missing else []
    return plan, notes


# ---------------------------------------------------------------- voice hook

_personalities: dict[str, str] | None = None


def voice_instruct(preset: str, base_url: str) -> str:
    """A VoiceStudio personality id (narrator, casual, ...) becomes its voice description; anything else is one."""
    global _personalities
    if _personalities is None:
        try:
            r = requests.get(f"{base_url}/personalities", timeout=5)
            _personalities = {p["id"]: p.get("instruct") or "" for p in r.json()}
        except (requests.RequestException, ValueError, KeyError, TypeError):
            _personalities = {}
    return _personalities.get(preset, preset)


def voice_line(spec: dict, st: dict) -> str | None:
    if not st["hook"]["voice"]:
        return None
    v = spec.get("voice_hook")
    if v is True:
        v = spec.get("hook_text")
    return v.strip() if isinstance(v, str) and v.strip() else None


def synth_voice(text: str, st: dict, work: Path) -> tuple[Path | None, str | None]:
    """Speak the hook line with a stock VoiceStudio voice (never a cloned one). (wav, note)."""
    base_url = os.environ.get("VOICESTUDIO_URL", "http://127.0.0.1:3900").rstrip("/")
    try:
        r = requests.post(f"{base_url}/generate", timeout=180, files={
            "text": (None, text), "instruct": (None, voice_instruct(st["hook"]["voicePreset"], base_url)),
            "effect_preset": (None, "raw")})
        r.raise_for_status()
        if not r.content.startswith(b"RIFF"):
            raise ValueError("VoiceStudio did not return audio")
    except (requests.RequestException, ValueError) as e:
        return None, f"voice hook skipped: VoiceStudio ({base_url}) {str(e)[:120]}"
    out = work / "voice_hook.wav"
    out.write_bytes(r.content)
    return out, None


# ---------------------------------------------------------------- audio graph

FMT = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
VOICE_DELAY = 0.15


def audio_graph(duration: float, music: dict | None = None, narration: dict | None = None,
                sfx: list[dict] = (), loop: bool = False) -> str:
    """Inputs: 0 = the clip, then the music (if any), the narration (if any), then each sound effect.

    The clip ducks under the narration; effects ride on top; the music ducks under the voice only
    (clip + narration), never under the effects. Output label [aout].
    """
    parts = [f"[0:a]{FMT}[clip]"]
    idx = 1 + bool(music)
    voice = "clip"
    if narration:
        d = int(VOICE_DELAY * 1000)
        parts += [f"[{idx}:a]{FMT},adelay={d}|{d},volume={narration['volume']:.2f},apad=whole_dur={duration:.3f},"
                  "asplit=2[nar][narkey]",
                  "[clip][narkey]sidechaincompress=threshold=0.02:ratio=10:attack=10:release=350[clipd]",
                  "[clipd][nar]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[voice]"]
        voice = "voice"
        idx += 1
    duck = bool(music and music["duck"])
    if duck:
        parts.append(f"[{voice}]asplit=2[vmix][vkey]")
        front = "vmix"
    else:
        front = voice
    if sfx:
        labels = []
        for i, e in enumerate(sfx):
            delay_ms = max(0, int(round(e["at"] * 1000)))
            trim = f"atrim=start={-e['at']:.3f},asetpts=PTS-STARTPTS," if e["at"] < 0 else ""
            parts.append(f"[{idx + i}:a]{FMT},{trim}adelay={delay_ms}|{delay_ms},volume={e['gain_db']:.2f}dB[s{i}]")
            labels.append(f"[s{i}]")
        parts.append(f"[{front}]{''.join(labels)}amix=inputs={len(sfx) + 1}:duration=first:dropout_transition=0:"
                     "normalize=0[front]")
        front = "front"
    if music:
        fade = 0.15 if loop else 1.5
        parts.append(f"[1:a]{FMT},atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,volume={music['gain_db']:.2f}dB,"
                     f"afade=t=in:d=0.4,afade=t=out:st={max(0.0, duration - fade):.3f}:d={fade}[mus]")
        if duck:
            parts.append(f"[mus][vkey]sidechaincompress=threshold={music['threshold']:.5f}:ratio=8:attack=15:"
                         "release=400[bed]")
        else:
            parts.append("[mus]anull[bed]")
        parts.append(f"[{front}][bed]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
    else:
        parts.append(f"[{front}]anull[aout]")
    return ";".join(parts)


# ---------------------------------------------------------------- video: crop, tracking, zoom

def zoom_filter(zooms: list[tuple[float, float, float]], cy: float, rate: str) -> str:
    """Punch-ins (t = seconds into the segment, duration, scale), easing in fast and out slower."""
    attack, release = 0.08, 0.18
    z = "1"
    for t0, dur, scale in zooms:
        a, d = t0 + attack, t0 + max(dur, attack)
        env = (f"if(between(it,{t0:.3f},{a:.3f}),(it-{t0:.3f})/{attack},"
               f"if(between(it,{a:.3f},{d:.3f}),1,"
               f"if(between(it,{d:.3f},{d + release:.3f}),1-(it-{d:.3f})/{release},0)))")
        z += f"+{scale - 1:.3f}*{env}"
    return (f"zoompan=z='{z}':x='iw/2-iw/zoom/2':y='(ih-ih/zoom)*{cy:.3f}':d=1:"
            f"s={OUT_W}x{OUT_H}:fps={rate}")


def segment_zooms(spec: dict, st: dict, r: dict, i: int, cold_len: float) -> list[tuple[float, float, float]]:
    z = st["zoom"]
    if not z["enabled"]:
        return []
    out = []
    for item in spec.get("zooms") or []:
        if not isinstance(item, dict):
            continue
        at = item.get("at", item.get("start"))   # {"start", "end"} reads as at + dur
        if at is None:
            continue
        at = float(at)
        dur = float(item["dur"]) if item.get("dur") is not None else (
            float(item["end"]) - at if item.get("end") is not None else 0.6)
        if r["start"] <= at < r["end"]:
            out.append((round(at - r["start"], 3), min(3.0, max(0.2, round(dur, 3))),
                        min(1.6, max(1.0, float(item.get("scale", z["scale"]))))))
    if z["auto"] and cold_len and i == 0:
        out.append((0.0, 0.6, z["scale"]))
    return sorted(out)


def segment_plans(spec: dict, st: dict, ranges: list[dict], source: Path, cold_len: float,
                  rate: str) -> tuple[list[str], list[str], list[str]]:
    """Each range's video filter (grade → layout/tracked crop → zoom), plus problems and notes."""
    layout = dict(spec.get("layout") or {"type": "blur"})
    kind = layout.get("type", "blur")
    problems, notes = [], []
    grade = ""
    if spec.get("grade"):
        g = spec["grade"]
        grade = get_preset(g) if g in ("subtle", "neutral_punch", "warm_cinematic", "none") else g
    can_track = tracker.available()
    if kind == "track" and not (can_track and st["framing"]["track"]):
        notes.append("tracking off or unavailable (opencv-python-headless): fixed centre crop")
    vfs = []
    for i, r in enumerate(ranges):
        tr = None
        if can_track and kind in ("track", "center", "split"):
            try:
                tr = tracker.track(source, r["start"], r["end"], float(layout.get("cx", 0.5)))
            except Exception as e:  # noqa: BLE001  - a tracking failure must not lose the render
                notes.append(f"tracking failed in segment {i}: {str(e)[:120]}")
        if kind == "track":
            x = tracker.x_expr(tr.keyframes) if tr and st["framing"]["track"] and tr.detected > 0 else None
            vf = layout_filter({"type": "center", "cx": layout.get("cx", 0.5)}, x)
        else:
            vf = layout_filter(layout)
        if tr and tr.detected >= 0.3:
            tracked = kind == "track" and st["framing"]["track"]
            cov_layout = {"type": "track"} if tracked else {"type": "center", "cx": layout.get("cx", 0.5)} if kind == "track" else layout
            cov = tracker.coverage(tr, cov_layout)
            if cov < 0.8:
                problems.append(f"subject out of frame in segment {i} ({cov:.0%} in view)")
            fy = tr.face_y()
            if fy and kind in ("track", "center") and st["captions"]["enabled"]:
                cap = 1 - st["captions"]["position"] / 100
                half = st["captions"]["size"] * 1.4 / OUT_H
                if cap - half < fy[1] and cap + half > fy[0]:
                    notes.append(f"captions cross the face in segment {i}: move them with style.captions.position")
        zooms = segment_zooms(spec, st, r, i, cold_len)
        if zooms:
            cy = tr.face_cy() if tr and kind in ("track", "center") else 0.5
            vf = f"{vf},{zoom_filter(zooms, cy, rate)}"
        vfs.append(f"{grade},{vf}" if grade else vf)
    return vfs, problems, notes


def extract_segments(ranges: list[dict], source: Path, vfs: list[str], work: Path, draft: bool, rate: str) -> list[Path]:
    """render.extract_segment per range with that range's own filter (extract_all_segments shares one)."""
    clips = work / ("clips_draft" if draft else "clips_graded")
    clips.mkdir(parents=True, exist_ok=True)
    print(f"extracting {len(ranges)} segment(s) → {clips.name}/  @ {rate} fps")
    out = []
    for i, (r, vf) in enumerate(zip(ranges, vfs)):
        path = clips / f"seg_{i:02d}.mp4"
        print(f"  [{i:02d}] {r['start']:7.2f}-{r['end']:7.2f}  ({r['end'] - r['start']:5.2f}s)")
        render.extract_segment(source, r["start"], r["end"] - r["start"], vf, path, preview=False, draft=draft, rate=rate)
        out.append(path)
    return out


# ---------------------------------------------------------------- composite

def _filter_path(p: Path) -> str:
    # as_posix: Windows backslashes are escape characters inside a filtergraph
    return p.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")


def composite(base: Path, ass: Path | None, audio: dict | None, out: Path, duration: float, draft: bool) -> None:
    """One pass: subtitles LAST on the video, the audio graph mixed. Replaces build_final_composite.

    audio: {"inputs": [(path, loop)], "graph": "...[aout]"} or None to keep the clip's audio as is.
    """
    cmd = ["ffmpeg", "-y", "-i", str(base)]
    for path, loop in (audio or {}).get("inputs", []):
        cmd += (["-stream_loop", "-1"] if loop else []) + ["-i", str(path)]
    graph = []
    if ass:
        fonts = _env.ROOT / "fonts"
        extra = f":fontsdir='{_filter_path(fonts)}'" if fonts.is_dir() else ""
        graph.append(f"[0:v]subtitles='{_filter_path(ass)}'{extra}[outv]")
    else:
        graph.append("[0:v]null[outv]")
    if audio:
        graph.append(audio["graph"])
    cmd += ["-filter_complex", ";".join(graph), "-map", "[outv]", "-map", "[aout]" if audio else "0:a",
            "-c:v", "libx264", "-preset", "ultrafast" if draft else "fast", "-crf", "28" if draft else "18",
            "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"] if audio else ["-c:a", "copy"]
    cmd += ["-t", f"{duration:.3f}", "-movflags", "+faststart", str(out)]
    names = [Path(p).name for p, _ in (audio or {}).get("inputs", [])]
    print(f"compositing → {out.name}  (subtitles: {'yes' if ass else 'no'}, audio: {', '.join(names) or 'clip only'})")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def render_clip(spec_path: Path, draft: bool = False, style_file: Path | None = None) -> dict:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    st = styles.resolve(styles.load(_env.ROOT, style_file), spec.get("style"))
    source = Path(spec["source"]).resolve()
    edit_dir = Path(spec["edit_dir"]).resolve()
    out_path = Path(spec["output"]).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    work = edit_dir / "shorts_work" / out_path.stem
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    # Source key = file stem, because the captions look up transcripts/<key>.json.
    key = source.stem
    ranges, cold_len = edit_ranges(spec, st)
    edl = {"version": 1, "sources": {key: str(source)}, "ranges": [{"source": key, **r} for r in ranges]}
    expected = sum(r["end"] - r["start"] for r in ranges)

    rate = render.probe_source_fps(source) or "24"
    vfs, problems, notes = segment_plans(spec, st, ranges, source, cold_len, rate)
    segs = extract_segments(ranges, source, vfs, work, draft, rate)
    base = work / "base.mp4"
    render.concat_segments(segs, base, work)

    ass_text = build_ass(edl, edit_dir, st, spec, expected, cold_len, spec.get("captions", True))
    ass = work / "short.ass"
    ass.write_text(ass_text, encoding="utf-8")
    has_events = "\nDialogue:" in ass_text

    music, note = plan_music(base, st, spec.get("music_mood"), out_path)
    notes += [note] if note else []
    narration = None
    line = voice_line(spec, st)
    if line:
        wav, note = synth_voice(line, st, work)
        notes += [note] if note else []
        if wav:
            narration = {"file": wav, "volume": st["hook"]["voiceVolume"]}
    effects, sfx_notes = plan_sfx(spec, st, ranges, cold_len, out_path)
    notes += sfx_notes

    audio = None
    if music or narration or effects:
        inputs = ([(music["track"], True)] if music else []) + ([(narration["file"], False)] if narration else [])
        inputs += [(e["file"], False) for e in effects]
        audio = {"inputs": inputs,
                 "graph": audio_graph(expected, music, narration, effects, st["hook"]["loop"])}

    prenorm = work / "prenorm.mp4"
    composite(base, ass if has_events else None, audio, prenorm, expected, draft)
    render.apply_loudnorm_two_pass(prenorm, out_path, preview=draft)

    info = probe(out_path)
    if (info["width"], info["height"]) != (OUT_W, OUT_H):
        problems.append(f"size {info['width']}x{info['height']}, expected {OUT_W}x{OUT_H}")
    if abs(info["duration"] - expected) > 0.5:
        problems.append(f"duration {info['duration']:.2f}s, expected {expected:.2f}s")
    lo, hi = st["clip"]["minSeconds"], st["clip"]["maxSeconds"]
    if not lo - 0.5 <= info["duration"] <= hi + 0.5:
        problems.append(f"length {info['duration']:.1f}s is outside {lo:g}-{hi:g}s")

    if spec.get("meta") is not None:
        meta = dict(spec["meta"])
        meta.update({
            "file": out_path.name,
            "duration_s": round(info["duration"], 2),
            "source_file": str(source),
            "source_ranges": spec["ranges"],
            "cold_open": spec.get("cold_open") if cold_len else None,
            "hook_text": spec.get("hook_text"),
            "voice_hook": line if narration else None,
            "music": music["track"].name if music else None,
            "sfx": [e["sound"] for e in effects],
            "spec": str(spec_path.resolve()),
            "layout": spec.get("layout"),
        })
        out_path.with_suffix(".json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    if not draft:
        shutil.rmtree(work, ignore_errors=True)
    return {"output": str(out_path), **info, "expected_duration": round(expected, 2), "problems": problems,
            "notes": notes}


def main() -> None:
    ap = argparse.ArgumentParser(description="Render 9:16 Shorts from clip specs")
    ap.add_argument("specs", type=Path, nargs="+", help="clip spec JSON file(s)")
    ap.add_argument("--jobs", type=int, default=3, help="parallel renders (default 3)")
    ap.add_argument("--draft", action="store_true", help="720p ultrafast, keeps work dir")
    ap.add_argument("--style", type=Path, help="style JSON to use instead of <workspace>/style.json")
    args = ap.parse_args()

    results, failed = [], 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(render_clip, p, args.draft, args.style): p for p in args.specs}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                r = fut.result()
                results.append(r)
                flag = "OK " if not r["problems"] else "BAD"
                print(f"[{flag}] {Path(r['output']).name}  {r['width']}x{r['height']}  "
                      f"{r['duration']:.2f}s  {'; '.join(r['problems'] + r['notes'])}")
                failed += bool(r["problems"])
            except Exception as e:  # report and keep going: one bad clip must not sink the batch
                failed += 1
                err = getattr(e, "stderr", None)
                detail = err.decode(errors="replace")[-600:] if isinstance(err, bytes) else str(e)
                print(f"[ERR] {p.name}: {detail}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
