"""Non-transcript signals for finding clip-worthy moments → sources/<slug>/edit/signals.md

  1. Loudness spikes: 1 s RMS windows; flags windows well above the source's own median
     (screams, hype, sudden laughter). Catches moments where nobody says anything quotable.
  2. Audio events: Scribe's (laughter) / (applause) / ... tags from the transcript.
     Local (WhisperX) transcripts have none; signals.md says so.
  3. Chat bursts: messages per 30 s from the chat replay, when fetch.py found one.

All times are in the downloaded video's own timeline (chat offsets are shifted by --section).

Usage:
    python clipper/signals.py sources/<slug>
"""

from __future__ import annotations

import _env  # noqa: F401

import argparse
import json
import subprocess
from pathlib import Path

import numpy as np

SR = 8000           # plenty for loudness, halves memory vs 16 kHz
WINDOW_S = 1.0
SPIKE_DB = 10.0     # this far above the median counts as a spike
MAX_LOUD = 40
CHAT_BUCKET_S = 30
MAX_CHAT = 30


def hms(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def loudness_db(video: Path) -> np.ndarray:
    """Per-window RMS in dBFS, streamed from ffmpeg so multi-hour VODs stay small in memory."""
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", str(SR),
         "-f", "s16le", "-"],
        stdout=subprocess.PIPE,
    )
    n = int(SR * WINDOW_S) * 2
    out: list[float] = []
    while True:
        buf = proc.stdout.read(n)
        if len(buf) < 2:
            break
        x = np.frombuffer(buf[: len(buf) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        out.append(20 * np.log10(rms) if rms > 1e-6 else -120.0)
    proc.wait()
    return np.array(out)


def loud_spikes(db: np.ndarray) -> tuple[float, list[tuple[float, float, float]]]:
    """Merge consecutive above-threshold windows into events: (start, end, peak dB over median)."""
    voiced = db[db > -70]
    median = float(np.median(voiced)) if voiced.size else -40.0
    hot = db > median + SPIKE_DB
    events, i = [], 0
    while i < len(db):
        if hot[i]:
            j = i
            while j + 1 < len(db) and hot[j + 1]:
                j += 1
            events.append((i * WINDOW_S, (j + 1) * WINDOW_S, float(db[i:j + 1].max() - median)))
            i = j + 1
        else:
            i += 1
    events.sort(key=lambda e: -e[2])
    return median, events[:MAX_LOUD]


def audio_events(transcript: Path) -> tuple[str | None, list[tuple[float, str]]]:
    """(engine, events). engine is None for Scribe transcripts, which don't record one."""
    if not transcript.exists():
        return None, []
    data = json.loads(transcript.read_text(encoding="utf-8"))
    return data.get("engine"), [(w["start"], (w.get("text") or "").strip())
                                for w in data.get("words", [])
                                if w.get("type") == "audio_event" and w.get("start") is not None]


def _offsets(node, out: list[float]) -> None:
    """Collect message times from either chat format: YouTube live_chat (videoOffsetTimeMsec)
    or Twitch rechat (content_offset_seconds)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "videoOffsetTimeMsec":
                out.append(float(v) / 1000.0)
            elif k == "content_offset_seconds":
                out.append(float(v))
            else:
                _offsets(v, out)
    elif isinstance(node, list):
        for v in node:
            _offsets(v, out)


def chat_times(paths: list[str]) -> list[float]:
    times: list[float] = []
    for p in paths:
        text = Path(p).read_text(encoding="utf-8", errors="replace")
        try:
            _offsets(json.loads(text), times)
        except json.JSONDecodeError:  # YouTube live_chat is JSON lines
            for line in text.splitlines():
                if line.strip():
                    try:
                        _offsets(json.loads(line), times)
                    except json.JSONDecodeError:
                        continue
    return sorted(times)


def section_start(section: str | None) -> float:
    if not section:
        return 0.0
    first = section.split("-")[0]
    secs = 0.0
    for part in first.split(":"):
        secs = secs * 60 + float(part or 0)
    return secs


def main() -> None:
    ap = argparse.ArgumentParser(description="Loudness / audio-event / chat signals for clip finding")
    ap.add_argument("source_dir", type=Path)
    args = ap.parse_args()

    src_dir = args.source_dir.resolve()
    info = json.loads((src_dir / "info.json").read_text(encoding="utf-8"))
    video = Path(info["video"])
    edit_dir = src_dir / "edit"
    edit_dir.mkdir(exist_ok=True)

    lines = [f"# Signals: {info.get('title')}", ""]

    db = loudness_db(video)
    median, spikes = loud_spikes(db)
    lines += [f"## Loudness spikes (median {median:.1f} dBFS; +{SPIKE_DB:.0f} dB or more; loudest first)", ""]
    lines += [f"- {hms(a)} [{a:.0f}-{b:.0f}s] +{d:.1f} dB" for a, b, d in spikes] or ["- none"]
    lines.append("")

    engine, events = audio_events(edit_dir / "transcripts" / f"{video.stem}.json")
    if engine:
        lines += ["## Audio events", "",
                  f"- unavailable: transcribed locally ({engine}), which doesn't tag laughter or applause. "
                  "Lean on loudness spikes and chat bursts instead.", ""]
    else:
        lines += [f"## Audio events from Scribe ({len(events)})", ""]
        lines += [f"- {hms(t)} [{t:.1f}s] {txt}" for t, txt in events] or ["- none (or not transcribed yet)"]
        lines.append("")

    chat = chat_times(info.get("chat") or [])
    lines += [f"## Chat bursts ({len(chat)} messages, {CHAT_BUCKET_S}s buckets)", ""]
    if chat:
        shift = section_start(info.get("section"))
        dur = len(db) * WINDOW_S
        buckets: dict[int, int] = {}
        for t in chat:
            t -= shift
            if 0 <= t <= dur:
                b = int(t // CHAT_BUCKET_S)
                buckets[b] = buckets.get(b, 0) + 1
        mean = (sum(buckets.values()) / max(1, dur / CHAT_BUCKET_S)) if buckets else 0
        top = sorted(((n, b) for b, n in buckets.items() if n >= 2 * mean), reverse=True)[:MAX_CHAT]
        lines.append(f"Average {mean:.1f} msgs per bucket; listing buckets at 2x or more.")
        lines += [f"- {hms(b * CHAT_BUCKET_S)} [{b * CHAT_BUCKET_S}-{(b + 1) * CHAT_BUCKET_S}s] "
                  f"{n} msgs ({n / mean:.1f}x)" for n, b in sorted(top, key=lambda x: x[1])] or ["- no bursts"]
    else:
        lines.append("- no chat replay for this source")
    lines.append("")

    out = edit_dir / "signals.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"SIGNALS {out}  ({len(spikes)} loud, {len(events)} audio events, {len(chat)} chat msgs)")


if __name__ == "__main__":
    main()
