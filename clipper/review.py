"""Review sheet per Short: the frames that decide whether it works, for Claude to Read in the self-check.

Per Short, one labelled row:
  0.3 s (the first frame a scroller sees) · end of the hook banner · each cut ±0.2 s · the payoff
  (where the cold open's moment plays again) · the last frame next to the first (the loop seam)
Cut times come from the meta JSON next to the Short (cold_open + source_ranges), so this runs on any
rendered Short without the work dir.

Usage:
    python clipper/review.py shorts/<slug>            → shorts/<slug>/_review_<NN>.png, one per Short
    python clipper/review.py shorts/<slug>/03_x.mp4   → just that one
"""

from __future__ import annotations

import _env  # noqa: F401

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import style as styles

THUMB_H = 480
MAX_CUTS = 4


def duration(video: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                         capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def moments(meta: dict, dur: float) -> list[tuple[str, float]]:
    """(label, output seconds) worth looking at, in order."""
    ranges = [r for r in meta.get("source_ranges") or [] if isinstance(r, dict)]
    cold = meta.get("cold_open")
    if cold:
        ranges = [cold] + ranges
    hook_end = styles.DEFAULTS["hook"]["seconds"]
    out = [("first look 0.3s", 0.3)]
    if meta.get("hook_text"):
        out.append(("hook ends", min(dur, hook_end) - 0.15))
    offset, cuts = 0.0, []
    for r in ranges[:-1]:
        offset += float(r["end"]) - float(r["start"])
        cuts.append(offset)
    for c in cuts[:MAX_CUTS]:
        out += [(f"cut {c:.1f}s -0.2", c - 0.2), (f"cut {c:.1f}s +0.2", c + 0.2)]
    if cold:
        src, offset = float(cold["start"]) + 0.5, 0.0
        for i, r in enumerate(ranges):
            if i and float(r["start"]) <= src < float(r["end"]):
                out.append(("payoff", offset + src - float(r["start"])))
                break
            offset += float(r["end"]) - float(r["start"])
    out += [("last frame", dur - 0.05), ("first frame (loop seam)", 0.0)]
    return [(label, min(max(0.0, t), dur - 0.04)) for label, t in out]


def sheet(video: Path, out: Path) -> Path:
    meta_path = video.with_suffix(".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    dur = duration(video)
    shots = moments(meta, dur)
    thumbs = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (_, t) in enumerate(shots):
            png = Path(tmp) / f"{i}.png"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                            "-vf", f"scale=-2:{THUMB_H}", str(png)], check=True)
            thumbs.append(Image.open(png).convert("RGB"))
    gap, label_h = 6, 26
    img = Image.new("RGB", (sum(t.width for t in thumbs) + gap * (len(thumbs) + 1), THUMB_H + label_h + 2 * gap),
                    (20, 20, 20))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 16)
    except OSError:
        font = ImageFont.load_default()
    x = gap
    for (label, t), th in zip(shots, thumbs):
        img.paste(th, (x, gap + label_h))
        draw.text((x + 2, gap + 3), f"{label} · {t:.1f}s", fill=(255, 220, 0), font=font)
        x += th.width + gap
    img.save(out)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Review sheets of rendered Shorts")
    ap.add_argument("target", type=Path, help="shorts/<slug> or one Short's mp4")
    args = ap.parse_args()
    videos = [args.target] if args.target.suffix == ".mp4" else sorted(args.target.glob("[0-9][0-9]_*.mp4"))
    if not videos:
        raise SystemExit(f"no Shorts in {args.target}")
    for v in videos:
        out = sheet(v, v.parent / f"_review_{v.name[:2]}.png")
        print(f"REVIEW {out}")


if __name__ == "__main__":
    main()
