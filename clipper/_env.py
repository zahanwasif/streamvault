"""Shared bootstrap for clipper scripts. Import first: `import _env`.

- Puts video-use/helpers on sys.path so we reuse its functions unmodified.
- Makes stdout/stderr UTF-8 (helpers print arrows; Windows consoles default to cp1252).
- Finds ffmpeg when the current process was started before ffmpeg landed on PATH.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # StreamVault/
VIDEO_USE = ROOT / "video-use"
HELPERS = VIDEO_USE / "helpers"
SOURCES = ROOT / "sources"
SHORTS = ROOT / "shorts"
INBOX = ROOT / "inbox"

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}

if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

if shutil.which("ffmpeg") is None:
    local = os.environ.get("LOCALAPPDATA", "")
    hits = glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "*", "bin"))
    if hits:
        os.environ["PATH"] = hits[0] + os.pathsep + os.environ.get("PATH", "")
if shutil.which("ffmpeg") is None:
    sys.exit("ffmpeg not found on PATH")


def slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (s[:max_len].rstrip("-")) or "untitled"
