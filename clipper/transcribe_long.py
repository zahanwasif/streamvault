"""Transcribe a registered source, locally on the GPU or with Scribe, chunking long VODs.

Writes the standard video-use transcript at sources/<slug>/edit/transcripts/<video stem>.json,
so pack_transcripts.py and render.build_master_srt work on it unchanged.

--engine local (default; the CLIPPER_ASR env var sets it): VoiceStudio's OpenAI-compatible
  endpoint (WhisperX on CUDA). 16 kHz mono audio goes up in 10-min chunks, one at a time
  (single GPU), each cached in transcripts/.chunks/<stem>.local.partNN.json. The merge is
  converted to Scribe's shape (word + spacing entries, speaker_id null). Free, but no audio
  events, no speaker IDs, and most "um"/"uh" are normalized away.

--engine scribe: ElevenLabs Scribe.
  <= 90 min : one Scribe call via video-use's transcribe_one (cached).
  >  90 min : 16 kHz mono audio is split into 60-min chunks, transcribed 4 at a time,
              and merged with time offsets. Each chunk's response is cached in
              transcripts/.chunks/, so a failure halfway never re-bills finished chunks.
              Speaker IDs restart per chunk (fine for picking clips).

Usage:
    python clipper/transcribe_long.py sources/<slug>
    python clipper/transcribe_long.py sources/<slug> --engine scribe --language en --num-speakers 2
"""

from __future__ import annotations

import _env  # noqa: F401

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from transcribe import call_scribe, extract_audio, load_api_key, peak_dbfs, transcribe_one, transcript_path

SINGLE_CALL_MAX_S = 90 * 60
CHUNK_S = 60 * 60

VOICESTUDIO_URL = os.environ.get("VOICESTUDIO_URL", "http://127.0.0.1:3900").rstrip("/")
LOCAL_CHUNK_S = 10 * 60
LOCAL_ENGINE = "voicestudio-whisperx"
# Whisper copies the style of its prompt; a filler-heavy one keeps more "um"/"uh"/laughs.
FILLER_PROMPT = "Umm, uh, like, you know... haha. So, um, yeah. Uh-huh. Hmm, wait, what? Haha!"


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


def call_voicestudio(wav: Path, language: str | None, prompt: str) -> dict:
    data = {
        "model": "whisper-1",  # means "the active engine"
        "response_format": "verbose_json",
        "timestamp_granularities[]": "word",
        "prompt": prompt,
    }
    if language:
        data["language"] = language
    with open(wav, "rb") as f:
        resp = requests.post(
            f"{VOICESTUDIO_URL}/v1/audio/transcriptions",
            files={"file": (wav.name, f, "audio/wav")},
            data=data,
            timeout=1800,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"VoiceStudio returned {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def to_scribe_words(raw: list[dict]) -> list[dict]:
    """Whisper words -> Scribe-shaped word + spacing entries.

    WhisperX can leave a word unaligned (often numbers); it borrows the previous word's end
    so captions and cuts still have a time to work with.
    """
    words: list[dict] = []
    for w in raw:
        text = (w.get("word") or w.get("text") or "").strip()
        if not text:
            continue
        start, end = w.get("start"), w.get("end")
        if start is None:
            start = words[-1]["end"] if words else 0.0
        if end is None or end < start:
            end = start
        words.append({"text": text, "start": round(start, 3), "end": round(end, 3),
                      "type": "word", "speaker_id": None})
    out: list[dict] = []
    for i, w in enumerate(words):
        if i:
            gap = words[i - 1]["end"]
            out.append({"text": " ", "start": gap, "end": max(gap, w["start"]),
                        "type": "spacing", "speaker_id": None})
        out.append(w)
    return out


def transcribe_local(video: Path, edit_dir: Path, total: float,
                     language: str | None, prompt: str) -> Path:
    try:
        requests.get(f"{VOICESTUDIO_URL}/.well-known/voicestudio-speech", timeout=5).raise_for_status()
    except requests.RequestException as e:
        sys.exit(f"VoiceStudio isn't answering at {VOICESTUDIO_URL} ({e}). "
                 "Start it, or rerun with --engine scribe.")

    out_path = transcript_path(edit_dir, video)
    chunk_dir = edit_dir / "transcripts" / ".chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    n_chunks = int(total // LOCAL_CHUNK_S) + (1 if total % LOCAL_CHUNK_S else 0)
    starts = [i * LOCAL_CHUNK_S for i in range(max(1, n_chunks))]

    t0 = time.time()
    parts: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        full = Path(tmp) / "full.wav"
        print(f"extracting audio ({total / 60:.1f} min)", flush=True)
        extract_audio(video, full)
        if peak_dbfs(full) < -60.0:
            sys.exit("audio track is silent - not transcribing. Check the source audio.")

        for i, offset in enumerate(starts):
            cached = chunk_dir / f"{video.stem}.local.part{i:02d}.json"
            if cached.exists():
                parts.append(json.loads(cached.read_text(encoding="utf-8")))
                continue
            wav = Path(tmp) / f"part{i:02d}.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(offset), "-t", str(LOCAL_CHUNK_S), "-i", str(full),
                 "-c", "copy", str(wav)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            print(f"  transcribing chunk {i + 1}/{len(starts)}", flush=True)
            payload = call_voicestudio(wav, language, prompt)
            cached.write_text(json.dumps(payload), encoding="utf-8")
            parts.append(payload)
    dt = time.time() - t0

    raw: list[dict] = []
    for offset, part in zip(starts, parts):
        for w in part.get("words") or []:
            w = dict(w)
            for k in ("start", "end"):
                if w.get(k) is not None:
                    w[k] = w[k] + offset
            raw.append(w)
    words = to_scribe_words(raw)
    merged = {
        "engine": LOCAL_ENGINE,
        "language_code": parts[0].get("language") if parts else None,
        "text": " ".join((p.get("text") or "").strip() for p in parts),
        "words": words,
        "chunked": {"chunk_seconds": LOCAL_CHUNK_S, "chunks": len(parts)},
    }
    out_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    n_words = sum(1 for w in words if w["type"] == "word")
    print(f"  saved: {out_path.name} ({n_words} words from {len(parts)} chunks) in {dt:.1f}s "
          f"= {dt / 60 / (total / 3600):.1f} min per audio hour")
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Transcription (local GPU or Scribe) with chunking for long VODs")
    ap.add_argument("source_dir", type=Path, help="sources/<slug> folder made by fetch.py")
    ap.add_argument("--engine", choices=["local", "scribe"], default=os.environ.get("CLIPPER_ASR", "local"),
                    help="local = VoiceStudio WhisperX (free); scribe = ElevenLabs (paid, has audio events)")
    ap.add_argument("--language", default=None, help="ISO code, e.g. en. Omit to auto-detect.")
    ap.add_argument("--num-speakers", type=int, default=None, help="scribe only")
    ap.add_argument("--prompt", default=FILLER_PROMPT, help="local only: Whisper style prompt")
    args = ap.parse_args()

    src_dir = args.source_dir.resolve()
    info = json.loads((src_dir / "info.json").read_text(encoding="utf-8"))
    video = Path(info["video"])
    edit_dir = src_dir / "edit"

    out = transcript_path(edit_dir, video)
    if out.exists():
        print(f"cached: {out}")
        return

    total = duration_of(video)
    if args.engine == "local":
        transcribe_local(video, edit_dir, total, args.language, args.prompt)
        print(f"TRANSCRIPT {out}")
        return

    api_key = load_api_key()
    if total <= SINGLE_CALL_MAX_S:
        transcribe_one(video, edit_dir, api_key, args.language, args.num_speakers)
    else:
        transcribe_chunked(video, edit_dir, api_key, total, args.language, args.num_speakers)
    print(f"TRANSCRIPT {out}")


if __name__ == "__main__":
    main()
