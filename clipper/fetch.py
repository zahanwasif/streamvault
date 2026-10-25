"""Register a source for clipping: download a URL, or point at local video files.

Every source gets its own folder:
    sources/<slug>/info.json     title, uploader, url, duration, "video" (absolute path)
    sources/<slug>/<slug>.mp4    downloaded video (URLs only; local files stay where they are)
    sources/<slug>/chat.*.json   chat replay when the platform has one (YouTube live_chat, Twitch rechat)
    sources/<slug>/edit/         transcripts and work files (created later)

Idempotent: a source that is already registered is reused, not downloaded again.
Prints one line per source:  SOURCE <dir>

Usage:
    python clipper/fetch.py <youtube-or-twitch-url>
    python clipper/fetch.py <url> --section 1:00:00-2:30:00     # only part of a long VOD
    python clipper/fetch.py <url> --cookies-from-browser chrome  # subscriber-only VODs
    python clipper/fetch.py <video-file-or-folder>
"""

from __future__ import annotations

import _env

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

FORMAT = (
    "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/"
    "b[height<=1080][ext=mp4]/"
    "bv*[height<=1080]+ba/b[height<=1080]/b"
)


def yt_dlp_base(args: argparse.Namespace) -> list[str]:
    exe = shutil.which("yt-dlp")
    if not exe:
        sys.exit("yt-dlp not found. Install it with: uv tool install \"yt-dlp[default]\"")
    cmd = [exe, "--no-playlist", "--js-runtimes", "node",
           "--ffmpeg-location", str(Path(shutil.which("ffmpeg")).parent)]
    if args.cookies_from_browser:
        cmd += ["--cookies-from-browser", args.cookies_from_browser]
    return cmd


def fetch_url(url: str, args: argparse.Namespace) -> Path:
    base = yt_dlp_base(args)
    probe = subprocess.run(base + ["-J", url], capture_output=True, text=True, encoding="utf-8")
    if probe.returncode != 0:
        sys.exit(f"yt-dlp could not read {url}:\n{probe.stderr.strip()[-1500:]}")
    meta = json.loads(probe.stdout)

    who = meta.get("uploader") or meta.get("channel") or meta.get("extractor_key", "")
    title = meta.get("title") or ""
    slug = _env.slugify(f"{who}-{title}", 50) + "-" + _env.slugify(str(meta["id"]), 20)
    if args.section:
        slug += "-" + _env.slugify(args.section, 20)
    src_dir = _env.SOURCES / slug
    src_dir.mkdir(parents=True, exist_ok=True)
    info_path = src_dir / "info.json"

    if info_path.exists():
        info = json.loads(info_path.read_text(encoding="utf-8"))
        if Path(info.get("video", "")).exists():
            print(f"already downloaded: {info['video']}")
            return src_dir

    cmd = base + ["-f", FORMAT, "--merge-output-format", "mp4", "--newline", "--progress-delta", "30",
                  "-o", str(src_dir / f"{slug}.%(ext)s"), url]
    if args.section:
        cmd += ["--download-sections", f"*{args.section}"]
    print(f"downloading {meta.get('title')} ({meta.get('duration_string', '?')})")
    if subprocess.run(cmd).returncode != 0:
        sys.exit("download failed. Subscriber-only or age-gated? Retry with --cookies-from-browser chrome")

    video = next((p for p in src_dir.glob(f"{slug}.*") if p.suffix.lower() in _env.VIDEO_EXTS), None)
    if video is None:
        sys.exit(f"download finished but no video file found in {src_dir}")

    # Chat replay is a bonus signal; most videos have none, so never fail on it.
    subprocess.run(
        base + ["--skip-download", "--write-subs", "--sub-langs", "live_chat,rechat",
                "-o", str(src_dir / "chat.%(ext)s"), url],
        capture_output=True,
    )

    info = {
        "title": meta.get("title"),
        "uploader": who,
        "channel_url": meta.get("channel_url") or meta.get("uploader_url"),
        "url": meta.get("webpage_url") or url,
        "id": meta.get("id"),
        "platform": meta.get("extractor_key"),
        "upload_date": meta.get("upload_date"),
        "duration": meta.get("duration"),
        "section": args.section,
        "video": str(video.resolve()),
        "chat": sorted(str(p.resolve()) for p in src_dir.glob("chat.*")),
    }
    info_path.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    return src_dir


def register_file(video: Path) -> Path:
    video = video.resolve()
    src_dir = _env.SOURCES / _env.slugify(video.stem, 60)
    src_dir.mkdir(parents=True, exist_ok=True)
    info_path = src_dir / "info.json"
    if not info_path.exists():
        dur = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True,
        ).stdout.strip()
        info = {
            "title": video.stem, "uploader": None, "url": None, "platform": "local",
            "duration": float(dur) if dur else None, "video": str(video), "chat": [],
        }
        info_path.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    return src_dir


def main() -> None:
    ap = argparse.ArgumentParser(description="Download or register a source for clipping")
    ap.add_argument("target", help="YouTube/Twitch URL, a video file, or a folder of videos")
    ap.add_argument("--section", help="Only this time range of the video, e.g. 1:00:00-2:30:00")
    ap.add_argument("--cookies-from-browser", help="Browser to borrow login cookies from (chrome, edge, firefox)")
    args = ap.parse_args()

    if args.target.startswith(("http://", "https://")):
        dirs = [fetch_url(args.target, args)]
    else:
        p = Path(args.target)
        if p.is_dir():
            files = sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in _env.VIDEO_EXTS)
            if not files:
                sys.exit(f"no videos in {p}")
            dirs = [register_file(f) for f in files]
        elif p.is_file():
            dirs = [register_file(p)]
        else:
            sys.exit(f"not a URL, file or folder: {args.target}")

    for d in dirs:
        print(f"SOURCE {d}")


if __name__ == "__main__":
    main()
