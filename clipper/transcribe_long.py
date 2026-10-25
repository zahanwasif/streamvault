"""Transcribe a registered source with Scribe, chunking long VODs.

Writes the standard video-use transcript at sources/<slug>/edit/transcripts/<video stem>.json,
so pack_transcripts.py and render.build_master_srt work on it unchanged.

  <= 90 min : one Scribe call via video-use's transcribe_one (cached).
  >  90 min : 16 kHz mono audio is split into 60-min chunks, transcribed 4 at a time,
              and merged with time offsets. Each chunk's response is cached in
              transcripts/.chunks/, so a failure halfway never re-bills finished chunks.
              Speaker IDs restart per chunk (fine for picking clips).

Usage:
    python clipper/transcribe_long.py sources/<slug>
    python clipper/transcribe_long.py sources/<slug> --language en --num-speakers 2
"""

from __future__ import annotations

import _env  # noqa: F401

import argparse
import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from transcribe import call_scribe, extract_audio, load_api_key, peak_dbfs, transcribe_one, transcript_path

SINGLE_CALL_MAX_S = 90 * 60
CHUNK_S = 60 * 60


def duration_of(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def transcribe_chunked(video: Path, edit_dir: Path, api_key: str, total: float,
                       language: str | None, num_speakers: int | None) -> Path:
    out_path = transcript_path(edit_dir, video)
    chunk_dir = edit_dir / "transcripts" / ".chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    starts = [i * CHUNK_S for i in range(int(total // CHUNK_S) + (1 if total % CHUNK_S else 0))]

    with tempfile.TemporaryDirectory() as tmp:
        full = Path(tmp) / "full.wav"
        print(f"extracting audio ({total / 3600:.1f} h)", flush=True)
        extract_audio(video, full)
        if peak_dbfs(full) < -60.0:
            sys.exit("audio track is silent - not uploading. Check the source audio.")

        def do_chunk(i: int) -> dict:
            cached = chunk_dir / f"{video.stem}.part{i:02d}.json"
            if cached.exists():
                return json.loads(cached.read_text(encoding="utf-8"))
            wav = Path(tmp) / f"part{i:02d}.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(starts[i]), "-t", str(CHUNK_S), "-i", str(full),
                 "-c", "copy", str(wav)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            print(f"  uploading chunk {i + 1}/{len(starts)}", flush=True)
            payload = call_scribe(wav, api_key, language, num_speakers)
            cached.write_text(json.dumps(payload), encoding="utf-8")
            return payload

        with ThreadPoolExecutor(max_workers=4) as pool:
            parts = list(pool.map(do_chunk, range(len(starts))))

    words: list[dict] = []
    for offset, part in zip(starts, parts):
        for w in part.get("words", []):
            w = dict(w)
            for k in ("start", "end"):
                if w.get(k) is not None:
                    w[k] = round(w[k] + offset, 3)
            words.append(w)
    merged = {
        "language_code": parts[0].get("language_code") if parts else None,
        "text": " ".join(p.get("text", "") for p in parts),
        "words": words,
        "chunked": {"chunk_seconds": CHUNK_S, "chunks": len(parts)},
    }
    out_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    print(f"  saved: {out_path.name} ({len(words)} tokens from {len(parts)} chunks)")
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Scribe transcription with chunking for long VODs")
    ap.add_argument("source_dir", type=Path, help="sources/<slug> folder made by fetch.py")
    ap.add_argument("--language", default=None, help="ISO code, e.g. en. Omit to auto-detect.")
    ap.add_argument("--num-speakers", type=int, default=None)
    args = ap.parse_args()

    src_dir = args.source_dir.resolve()
    info = json.loads((src_dir / "info.json").read_text(encoding="utf-8"))
    video = Path(info["video"])
    edit_dir = src_dir / "edit"

    out = transcript_path(edit_dir, video)
    if out.exists():
        print(f"cached: {out}")
        return

    api_key = load_api_key()
    total = duration_of(video)
    if total <= SINGLE_CALL_MAX_S:
        transcribe_one(video, edit_dir, api_key, args.language, args.num_speakers)
    else:
        transcribe_chunked(video, edit_dir, api_key, total, args.language, args.num_speakers)
    print(f"TRANSCRIPT {out}")


if __name__ == "__main__":
    main()
