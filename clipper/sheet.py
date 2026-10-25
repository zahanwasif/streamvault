"""Contact sheet: one frame from every Short in a folder, side by side, numbered.

Usage:
    python clipper/sheet.py shorts/<slug> [--at 1.0]   → shorts/<slug>/_sheet.png
"""

from __future__ import annotations

import _env  # noqa: F401

import argparse
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

THUMB_H = 640


def main() -> None:
    ap = argparse.ArgumentParser(description="Contact sheet of Shorts")
    ap.add_argument("folder", type=Path)
    ap.add_argument("--at", type=float, default=1.0, help="timestamp to grab (s)")
    args = ap.parse_args()

    clips = sorted(args.folder.glob("*.mp4"))
    if not clips:
        raise SystemExit(f"no mp4 in {args.folder}")

    thumbs = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, c in enumerate(clips):
            png = Path(tmp) / f"{i}.png"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(args.at), "-i", str(c),
                            "-frames:v", "1", "-vf", f"scale=-2:{THUMB_H}", str(png)], check=True)
            thumbs.append(Image.open(png).convert("RGB"))

    gap = 8
    sheet = Image.new("RGB", (sum(t.width for t in thumbs) + gap * (len(thumbs) + 1), THUMB_H + 2 * gap), (20, 20, 20))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    x = gap
    for c, t in zip(clips, thumbs):
        sheet.paste(t, (x, gap))
        draw.text((x + 8, gap + 6), c.name[:2], fill=(255, 220, 0), font=font, stroke_width=2, stroke_fill=(0, 0, 0))
        x += t.width + gap

    out = args.folder / "_sheet.png"
    sheet.save(out)
    print(f"SHEET {out}  ({len(clips)} clips)")


if __name__ == "__main__":
    main()
