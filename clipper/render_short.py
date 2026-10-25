"""Render one or more 9:16 YouTube Shorts from clip specs.

Composes video-use's render.py functions unmodified:
  extract_all_segments (30ms fades, layout via the EDL `grade` field)
  → concat_segments (lossless)
  → build_master_srt (output-timeline captions from cached Scribe words)
  → build_final_composite (subtitles LAST)
  → apply_loudnorm_two_pass (-14 LUFS / -1 dBTP)

Each clip renders in its own work dir, so several can run in parallel.

Clip spec (JSON):
{
  "source":   "C:/.../sources/<slug>/video.mp4",
  "edit_dir": "C:/.../sources/<slug>/edit",          # holds transcripts/<source stem>.json
  "ranges":   [{"start": 812.40, "end": 838.10}, ...],  # source seconds; >1 range = jump cuts
  "layout":   {"type": "split", "cam": [x, y, w, h], "game": [x, y, w, h], "cam_height": 768}
            | {"type": "center", "cx": 0.5}             # cx = speaker centre, fraction of width
            | {"type": "blur"}
            | {"type": "fit"},                          # already-vertical sources
  "grade":    "neutral_punch",                         # optional video-use preset or raw filter
  "captions": true,                                    # default true
  "output":   "C:/.../shorts/<slug>/01_funny_title.mp4",
  "meta":     {...}                                    # optional; written next to output as .json
}
Boxes are fractions (0..1) of the source frame, so they hold for any resolution.

Usage:
    python clipper/render_short.py clip.json [clip2.json ...] [--jobs 3] [--draft]
"""

from __future__ import annotations

import _env  # noqa: F401  (must come first: sys.path, PATH, UTF-8)

import argparse
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import render
from grade import get_preset

OUT_W, OUT_H = 1080, 1920

# Shorts captions: Helvetica is absent on Windows, so Arial. libass scales from
# PlayResY=288, so FontSize 14 ≈ 93px on a 1920-tall frame; MarginV=90 keeps the
# baseline ~30% up, clear of the Shorts UI (see render.py's note).
SHORTS_SUB_STYLE = (
    "FontName=Arial,FontSize=14,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
    "BorderStyle=1,Outline=3,Shadow=0,"
    "Alignment=2,MarginV=90"
)


def _fill(w: int, h: int) -> str:
    """Scale to cover w×h, then centre-crop to exactly w×h."""
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"


def _crop_box(box: list[float]) -> str:
    x, y, w, h = box
    return f"crop=w=iw*{w}:h=ih*{h}:x=iw*{x}:y=ih*{y}"


def layout_filter(layout: dict) -> str:
    kind = layout.get("type", "blur")
    if kind == "center":
        cx = float(layout.get("cx", 0.5))
        return (
            f"crop=w=ih*9/16:h=ih:x='max(0,min(iw-ow,{cx}*iw-ow/2))':y=0,"
            f"scale={OUT_W}:{OUT_H}"
        )
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


def render_clip(spec_path: Path, draft: bool = False) -> dict:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    source = Path(spec["source"]).resolve()
    edit_dir = Path(spec["edit_dir"]).resolve()
    out_path = Path(spec["output"]).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    work = edit_dir / "shorts_work" / out_path.stem
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    vf = layout_filter(spec.get("layout") or {"type": "blur"})
    if spec.get("grade"):
        g = spec["grade"]
        color = get_preset(g) if g in ("subtle", "neutral_punch", "warm_cinematic", "none") else g
        if color:
            vf = f"{color},{vf}"

    # Source key = file stem, because build_master_srt looks up transcripts/<key>.json.
    key = source.stem
    edl = {
        "version": 1,
        "sources": {key: str(source)},
        "ranges": [{"source": key, "start": float(r["start"]), "end": float(r["end"])}
                   for r in spec["ranges"]],
        "grade": vf,
    }
    expected = sum(r["end"] - r["start"] for r in edl["ranges"])

    segs = render.extract_all_segments(edl, work, preview=False, draft=draft)
    base = work / "base.mp4"
    render.concat_segments(segs, base, work)

    subs = None
    if spec.get("captions", True):
        subs = work / "master.srt"
        render.build_master_srt(edl, edit_dir, subs)

    if subs is not None:
        render.SUB_FORCE_STYLE = SHORTS_SUB_STYLE
        prenorm = work / "prenorm.mp4"
        render.build_final_composite(base, [], subs, prenorm, work)
    else:
        prenorm = base  # nothing to composite; render.py's copy path floods the log
    render.apply_loudnorm_two_pass(prenorm, out_path, preview=draft)

    info = probe(out_path)
    problems = []
    if (info["width"], info["height"]) != (OUT_W, OUT_H):
        problems.append(f"size {info['width']}x{info['height']}, expected {OUT_W}x{OUT_H}")
    if abs(info["duration"] - expected) > 0.5:
        problems.append(f"duration {info['duration']:.2f}s, expected {expected:.2f}s")

    if spec.get("meta") is not None:
        meta = dict(spec["meta"])
        meta.update({
            "file": out_path.name,
            "duration_s": round(info["duration"], 2),
            "source_file": str(source),
            "source_ranges": spec["ranges"],
            "layout": spec.get("layout"),
        })
        out_path.with_suffix(".json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    if not draft:
        shutil.rmtree(work, ignore_errors=True)
    return {"output": str(out_path), **info, "expected_duration": round(expected, 2), "problems": problems}


def main() -> None:
    ap = argparse.ArgumentParser(description="Render 9:16 Shorts from clip specs")
    ap.add_argument("specs", type=Path, nargs="+", help="clip spec JSON file(s)")
    ap.add_argument("--jobs", type=int, default=3, help="parallel renders (default 3)")
    ap.add_argument("--draft", action="store_true", help="720p ultrafast, keeps work dir")
    args = ap.parse_args()

    results, failed = [], 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(render_clip, p, args.draft): p for p in args.specs}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                r = fut.result()
                results.append(r)
                flag = "OK " if not r["problems"] else "BAD"
                print(f"[{flag}] {Path(r['output']).name}  {r['width']}x{r['height']}  "
                      f"{r['duration']:.2f}s  {'; '.join(r['problems'])}")
                failed += bool(r["problems"])
            except Exception as e:  # report and keep going: one bad clip must not sink the batch
                failed += 1
                err = getattr(e, "stderr", None)
                detail = err.decode(errors="replace")[-600:] if isinstance(err, bytes) else str(e)
                print(f"[ERR] {p.name}: {detail}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
