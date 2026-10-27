"""The sound library in the workspace: music/<mood>/ and sfx/<category>/, each with a library.json.

music/library.json  {"tracks": [{file, mood, kind: generated|imported, source, title, prompt, model,
                                 bpm, energy_db, lufs, duration, added}]}
sfx/library.json    {"sounds": [{file, category, tags, prompt, model, hit_ms, lufs, duration, added}]}

render_short.py reads these (sfx hit_ms aligns each effect's peak on its moment; lufs levels it).
Both files are the record of where every sound came from: keep them for disputing a copyright claim.
"""

from __future__ import annotations

import _env  # noqa: F401

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import numpy as np

MOODS = ("funny", "hype", "romantic", "chill")
SFX_CATEGORIES = ("transition", "impact", "reaction", "ui")


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_library(path: Path, key: str) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [e for e in data.get(key, []) if isinstance(e, dict)]
    except (OSError, ValueError, AttributeError):
        return []


def write_library(path: Path, key: str, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({key: sorted(entries, key=lambda e: e["file"])}, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def upsert(path: Path, key: str, entry: dict) -> None:
    entries = [e for e in read_library(path, key) if e.get("file") != entry["file"]]
    write_library(path, key, entries + [entry])


def load_mono(path: Path, sr: int = 22050) -> np.ndarray:
    """Decode with ffmpeg (any format) to mono float32."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                         capture_output=True, check=True)
    return np.frombuffer(out.stdout, np.float32)


def rms_db(y: np.ndarray, sr: int = 22050, hop: int = 512) -> np.ndarray:
    n = max(1, len(y) // hop)
    frames = y[: n * hop].reshape(n, hop) if len(y) >= hop else y.reshape(1, -1)
    return 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)


def lufs(path: Path) -> float | None:
    import render
    m = render.measure_loudness(path)
    try:
        v = float(m["input_i"]) if m else None
    except (TypeError, ValueError):
        return None
    return round(v, 1) if v is not None and np.isfinite(v) and v > -70 else None


def peak_ms(y: np.ndarray, sr: int = 22050, hop: int = 512) -> int:
    """When the effect hits hardest: a boom's attack, a whoosh's middle, a riser's end."""
    env = rms_db(y, sr, hop)
    return int(round(int(np.argmax(env)) * hop / sr * 1000))


def analyze_music(y: np.ndarray, sr: int = 22050) -> dict:
    """Tempo, loudness and brightness, plus a guess at the mood (the user can move the file)."""
    import librosa
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    bpm = float(np.atleast_1d(tempo)[0])
    energy = float(np.median(rms_db(y, sr)))
    centroid = float(np.median(librosa.feature.spectral_centroid(y=y, sr=sr)))
    onsets = len(librosa.onset.onset_detect(y=y, sr=sr)) / max(1.0, len(y) / sr)
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr).mean(axis=1)
    major = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    minor = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
    best = lambda prof: max(np.corrcoef(np.roll(prof, k), chroma)[0, 1] for k in range(12))  # noqa: E731
    is_major = best(major) >= best(minor)
    return {"bpm": round(bpm), "energy_db": round(energy, 1), "brightness_hz": round(centroid),
            "onsets_per_s": round(onsets, 2), "major": bool(is_major), "mood": guess_mood(bpm, energy, onsets, is_major)}


def guess_mood(bpm: float, energy_db: float, onsets: float, major: bool) -> str:
    # Half-time detections: phonk at 130 often reads as 65.
    fast = bpm >= 115 or (bpm < 80 and onsets > 3.5)
    loud = energy_db > -18
    if fast and loud and not major:
        return "hype"
    if fast or (loud and onsets > 3):
        return "funny" if major else "hype"
    if major and onsets < 2.5:
        return "romantic"
    return "chill"


def trim_intro(path: Path, max_lead_s: float = 0.5) -> float:
    """Cut a quiet intro so the track hits within max_lead_s (music starts at 0:00 in every Short)."""
    y = load_mono(path)
    env = rms_db(y)
    if not len(env):
        return 0.0
    loud = np.nonzero(env > env.max() - 18)[0]
    start = loud[0] * 512 / 22050 if len(loud) else 0.0
    if start <= max_lead_s:
        return 0.0
    cut = round(start - 0.15, 2)
    tmp = path.with_name(path.stem + ".trim" + path.suffix)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{cut}", "-i", str(path), "-c:a", "libmp3lame", "-q:a", "2",
                    str(tmp)], check=True)
    os.replace(tmp, path)
    return cut


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True)
    return round(float(out.stdout.strip()), 2)
