"""Add music to the workspace library from links or files the user says are cleared to use.

Each link's audio is downloaded with yt-dlp; files are copied. The track is checked for talking (a
reel's audio often has people speaking over the music): VoiceStudio transcribes the first minute,
and a track with speech goes to music/_review/ instead of being used. Clean tracks get their quiet
intro trimmed, are sorted into a mood (tempo, energy, key; --mood overrides), and are listed in
music/library.json with the source link, which is the record for disputing a copyright claim.

Usage:
    python clipper/music_import.py <url|file> [...] [--mood hype] [--keep-speech]
Prints one line per track: ADDED music/<mood>/<file>  or  REVIEW music/_review/<file>  (why).
"""

from __future__ import annotations

import _env

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import requests

import sounds

MUSIC = _env.ROOT / "music"
SPEECH_WORDS_PER_S = 0.6     # more than this over the first minute means people are talking over it


def download(url: str, dest: Path) -> tuple[Path, str]:
    exe = shutil.which("yt-dlp")
    if not exe:
        sys.exit("yt-dlp not found. Install it with: uv tool install \"yt-dlp[default]\"")
    dest.mkdir(parents=True, exist_ok=True)
    out = subprocess.run([exe, "--no-playlist", "-f", "bestaudio/best", "-x", "--audio-format", "mp3",
                          "--audio-quality", "0", "-o", str(dest / "%(id)s.%(ext)s"),
                          "--print", "after_move:filepath", "--print", "title", url],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    if out.returncode != 0 or not lines:
        raise RuntimeError(f"yt-dlp could not get {url}: {out.stderr.strip()[-300:]}")
    title = lines[0] if len(lines) > 1 else Path(lines[-1]).stem
    return Path(lines[-1]), title


def speech_rate(path: Path) -> float | None:
    """Words per second VoiceStudio hears in the first minute; None if it isn't answering."""
    base = os.environ.get("VOICESTUDIO_URL", "http://127.0.0.1:3900").rstrip("/")
    clip = path.with_name(path.stem + ".probe.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-t", "60", "-ac", "1", "-ar", "16000", str(clip)],
                   check=True)
    try:
        with open(clip, "rb") as fh:
            r = requests.post(f"{base}/v1/audio/transcriptions", files={"file": fh}, data={"model": "whisper-1"},
                              timeout=300)
        r.raise_for_status()
        words = len(re.findall(r"[A-Za-z']{2,}", r.json().get("text", "")))
        return words / max(1.0, min(60.0, sounds.duration(path)))
    except (requests.RequestException, ValueError):
        return None
    finally:
        clip.unlink(missing_ok=True)


def slug(text: str) -> str:
    return _env.slugify(text, 40)


def add(target: str, mood: str | None, keep_speech: bool) -> str:
    incoming = MUSIC / "_incoming"
    if re.match(r"https?://", target):
        path, title = download(target, incoming)
        source = target
    else:
        src = Path(target)
        if not src.exists():
            raise RuntimeError(f"no such file: {target}")
        incoming.mkdir(parents=True, exist_ok=True)
        path = incoming / src.name
        shutil.copy2(src, path)
        title, source = src.stem, str(src.resolve())

    rate = None if keep_speech else speech_rate(path)
    if rate is not None and rate > SPEECH_WORDS_PER_S:
        review = MUSIC / "_review" / f"{slug(title)}{path.suffix}"
        review.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), review)
        return (f"REVIEW music/_review/{review.name}  (people talk over it: {rate:.1f} words/s; "
                "share the song's own link, or rerun with --keep-speech)")

    cut = sounds.trim_intro(path)
    info = sounds.analyze_music(sounds.load_mono(path))
    chosen = mood or info["mood"]
    final = MUSIC / chosen / f"{slug(title)}{path.suffix}"
    final.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), final)
    sounds.upsert(MUSIC / "library.json", "tracks", {
        "file": f"{chosen}/{final.name}", "mood": chosen, "kind": "imported", "source": source, "title": title,
        "bpm": info["bpm"], "energy_db": info["energy_db"], "major": info["major"], "lufs": sounds.lufs(final),
        "duration": sounds.duration(final), "intro_trimmed_s": cut, "speech_words_per_s": rate,
        "mood_from": "user" if mood else "analysis", "added": sounds.now()})
    return f"ADDED music/{chosen}/{final.name}  ({info['bpm']} BPM, {'major' if info['major'] else 'minor'}, " \
           f"{'mood set by you' if mood else 'mood guessed from tempo/energy/key'})"


def main() -> int:
    ap = argparse.ArgumentParser(description="Import cleared music from links or files into music/<mood>/")
    ap.add_argument("targets", nargs="+", help="YouTube/YouTube Music/reel links or audio/video files")
    ap.add_argument("--mood", choices=sounds.MOODS, help="put them all in this mood instead of guessing")
    ap.add_argument("--keep-speech", action="store_true", help="import even if people talk over the music")
    args = ap.parse_args()
    failed = 0
    for t in args.targets:
        try:
            print(add(t, args.mood, args.keep_speech), flush=True)
        except Exception as e:  # noqa: BLE001  - one bad link must not stop the rest
            failed += 1
            print(f"FAILED {t}: {e}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
