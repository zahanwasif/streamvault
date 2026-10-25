"""Print word-level timings for a time range, for snapping clip edges to word boundaries.

Each line: start-end  gap-before  text   (audio events in parentheses; gaps >= 0.4 s marked)

Usage:
    python clipper/words.py sources/<slug> 812 840
"""

from __future__ import annotations

import _env  # noqa: F401

import argparse
import json
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description="Word timings in a range")
    ap.add_argument("source_dir", type=Path)
    ap.add_argument("start", type=float)
    ap.add_argument("end", type=float)
    args = ap.parse_args()

    src_dir = args.source_dir.resolve()
    info = json.loads((src_dir / "info.json").read_text(encoding="utf-8"))
    tr = src_dir / "edit" / "transcripts" / f"{Path(info['video']).stem}.json"
    words = json.loads(tr.read_text(encoding="utf-8")).get("words", [])

    prev_end = None
    for w in words:
        if w.get("type") == "spacing" or w.get("start") is None:
            continue
        s, e = w["start"], w.get("end", w["start"])
        if e < args.start or s > args.end:
            prev_end = e
            continue
        gap = s - prev_end if prev_end is not None else 0.0
        mark = "  <-- gap" if gap >= 0.4 else ""
        text = (w.get("text") or "").strip()
        if w.get("type") == "audio_event" and not text.startswith("("):
            text = f"({text})"
        spk = str(w.get("speaker_id") or "").replace("speaker_", "S")
        print(f"{s:9.2f}-{e:9.2f}  +{gap:5.2f}  {spk:>3} {text}{mark}")
        prev_end = e


if __name__ == "__main__":
    main()
