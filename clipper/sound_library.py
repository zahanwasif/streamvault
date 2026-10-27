"""Generate the workspace's music and sound effects with ElevenLabs (the user's own API key and plan).

The app's Setup step runs this with ELEVENLABS_API_KEY set. Files that already exist are skipped, so
running it again only fills gaps (or use --force). Each file is analysed and listed in
music/library.json or sfx/library.json (see sounds.py).

ElevenLabs allows commercial use (monetized YouTube included) of generated music and effects on its
paid plans, not on the free plan.

Usage:
    python clipper/sound_library.py --check                 # key, plan and credits
    python clipper/sound_library.py --sfx --music           # everything missing
    python clipper/sound_library.py --music --only hype_phonk_1,funny_bounce_1 --force
    python clipper/sound_library.py --list                  # the catalogue, nothing generated
"""

from __future__ import annotations

import _env

import argparse
import os
import sys
from pathlib import Path

import requests

import sounds

API = "https://api.elevenlabs.io"
NO_INTRO = "Starts immediately on the full beat, no intro, no vocals."

# (id, category, prompt, seconds). Generic, original sounds: no imitations of real songs or people.
SFX = [
    ("whoosh_1", "transition", "Fast airy whoosh swish passing by, clean, very short tail", 0.8),
    ("whoosh_2", "transition", "Deep cinematic whoosh with a low rumble, quick pass-by", 1.0),
    ("whoosh_3", "transition", "Quick sharp swipe whoosh, bright and snappy", 0.6),
    ("swoosh_1", "transition", "Soft smooth swoosh transition, gentle air movement", 0.7),
    ("glitch_1", "transition", "Short digital glitch stutter transition, electronic", 0.6),
    ("riser_1", "transition", "Tension riser building for two seconds and ending abruptly on its peak, cinematic", 2.5),
    ("riser_2", "transition", "Fast reverse cymbal swell rising into a hit", 1.5),
    ("boom_1", "impact", "Deep sub bass boom impact, punchy 808 drop, cinematic", 1.5),
    ("boom_2", "impact", "Heavy distorted bass hit, dramatic meme-style boom", 1.5),
    ("hit_1", "impact", "Punchy cinematic hit with a short reverb", 1.0),
    ("thud_1", "impact", "Dull heavy cartoon thud, something hitting the floor", 0.8),
    ("gasp_1", "reaction", "Small studio crowd gasping in shock", 1.5),
    ("laugh_1", "reaction", "Studio audience laughing, short sitcom laugh track", 2.5),
    ("ooh_1", "reaction", "Crowd going 'oooh' in surprise", 1.5),
    ("scratch_1", "reaction", "Vinyl record scratch, abrupt stop", 0.8),
    ("boing_1", "reaction", "Cartoon spring boing, comedic", 0.8),
    ("ding_1", "reaction", "Bright bell ding, correct answer, short", 0.8),
    ("sting_1", "reaction", "Dramatic suspense string sting, short orchestral stab", 1.5),
    ("airhorn_1", "reaction", "Air horn blast, short party horn", 1.2),
    ("crickets_1", "reaction", "Crickets chirping in an awkward silence", 2.0),
    ("pop_1", "ui", "Short bubbly pop sound", 0.5),
    ("pop_2", "ui", "Soft mouth pop click", 0.5),
    ("click_1", "ui", "Crisp camera shutter click", 0.5),
]

# (id, mood, prompt). All instrumental, ~60 s: a Short is at most a minute and the renderer loops.
MUSIC = [
    ("hype_phonk_1", "hype", "Aggressive drift phonk, 130 BPM, distorted 808 bass, cowbell melody, hard-hitting drums, dark and energetic."),
    ("hype_phonk_2", "hype", "Dark Memphis-style phonk, 125 BPM, gritty cowbell riff, heavy saturated 808, menacing."),
    ("hype_trap_1", "hype", "Hard trap beat, 140 BPM, booming 808s, fast hi-hat rolls, dark synth lead, intense."),
    ("hype_funk_1", "hype", "Brazilian phonk funk, 128 BPM, heavy bass, punchy kicks, cowbell, energetic."),
    ("hype_epic_1", "hype", "Epic hype trap, orchestral hits over hard 808 drums, 150 BPM, triumphant."),
    ("funny_bounce_1", "funny", "Bouncy playful phonk, 120 BPM, quirky cowbell, wobbly bass, comedic."),
    ("funny_cartoon_1", "funny", "Goofy cartoon hip-hop beat, pizzicato strings and tuba bass, 100 BPM, funny."),
    ("funny_sneaky_1", "funny", "Sneaky comedic trap beat, plucky synth, staccato, 95 BPM, mischievous."),
    ("funny_funk_1", "funny", "Silly upbeat funk groove, slap bass, quirky lead, 110 BPM."),
    ("funny_lofi_1", "funny", "Playful lo-fi meme beat, chopped bright melody, 90 BPM, light-hearted."),
    ("romantic_rnb_1", "romantic", "Smooth R&B, 80 BPM, warm electric piano, soft bass, romantic."),
    ("romantic_lofi_1", "romantic", "Romantic lo-fi beat, mellow guitar, vinyl crackle, 75 BPM, tender."),
    ("romantic_pop_1", "romantic", "Dreamy pop ballad, soft synth pads, gentle drums, 85 BPM."),
    ("romantic_acoustic_1", "romantic", "Sweet acoustic guitar love theme, light percussion, 90 BPM."),
    ("romantic_slow_1", "romantic", "Slow jam R&B beat, silky keys, 70 BPM, warm."),
    ("chill_lofi_1", "chill", "Chill lo-fi hip-hop, 85 BPM, jazzy piano, dusty drums, relaxed."),
    ("chill_hop_1", "chill", "Ambient chillhop, soft Rhodes, mellow bass, 80 BPM."),
    ("chill_summer_1", "chill", "Laid-back summer beat, warm guitar, light percussion, 90 BPM."),
    ("chill_downtempo_1", "chill", "Calm downtempo electronic, airy pads, soft kick, 95 BPM."),
    ("chill_trap_1", "chill", "Chill trap beat, soft 808, airy pads, 70 BPM, dreamy."),
]
SFX_MIN_S, SFX_MAX_S = 0.5, 30
MUSIC_SECONDS = 60
MUSIC_MODEL = "music_v1"
SFX_MODEL = "eleven_text_to_sound_v2"


class ApiError(Exception):
    pass


def call(key: str, path: str, json_body: dict, timeout: float) -> bytes:
    r = requests.post(f"{API}{path}", params={"output_format": "mp3_44100_128"}, json=json_body, timeout=timeout,
                      headers={"xi-api-key": key, "Content-Type": "application/json"})
    if r.status_code == 401:
        raise ApiError("ElevenLabs rejected the API key (401): check it in Setup")
    if r.status_code in (402, 429) or (r.status_code == 400 and "quota" in r.text.lower()):
        raise ApiError(f"ElevenLabs: out of credits or rate-limited ({r.status_code}): {r.text[:200]}")
    if r.status_code != 200:
        raise ApiError(f"ElevenLabs {path} failed ({r.status_code}): {r.text[:300]}")
    return r.content


def check(key: str) -> str:
    r = requests.get(f"{API}/v1/user/subscription", headers={"xi-api-key": key}, timeout=20)
    if r.status_code == 401:
        raise ApiError("ElevenLabs rejected the API key (401)")
    r.raise_for_status()
    s = r.json()
    used, limit = s.get("character_count", 0), s.get("character_limit", 0)
    tier = s.get("tier", "?")
    note = " (free plan: no commercial use)" if tier == "free" else ""
    return f"plan {tier}{note}, {max(0, limit - used):,} of {limit:,} credits left"


def make_sfx(key: str, folder: Path, ids: set[str] | None, force: bool) -> int:
    made = 0
    for sid, cat, prompt, secs in SFX:
        if ids and sid not in ids:
            continue
        out = folder / cat / f"{sid}.mp3"
        if out.exists() and not force:
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        secs = min(SFX_MAX_S, max(SFX_MIN_S, secs))   # the API's limits
        out.write_bytes(call(key, "/v1/sound-generation", {"text": prompt, "duration_seconds": secs,
                                                           "prompt_influence": 0.5, "model_id": SFX_MODEL}, 120))
        y = sounds.load_mono(out)
        sounds.upsert(folder / "library.json", "sounds", {
            "file": f"{cat}/{sid}.mp3", "category": cat, "tags": [sid.rsplit("_", 1)[0]], "prompt": prompt,
            "model": f"elevenlabs:{SFX_MODEL}", "hit_ms": sounds.peak_ms(y), "lufs": sounds.lufs(out),
            "duration": sounds.duration(out), "added": sounds.now()})
        print(f"MADE sfx/{cat}/{sid}.mp3", flush=True)
        made += 1
    return made


def make_music(key: str, folder: Path, ids: set[str] | None, force: bool) -> int:
    made = 0
    for mid, mood, prompt in MUSIC:
        if ids and mid not in ids:
            continue
        out = folder / mood / f"ai_{mid}.mp3"
        if out.exists() and not force:
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        full = f"{prompt} {NO_INTRO}"
        out.write_bytes(call(key, "/v1/music", {"prompt": full, "music_length_ms": MUSIC_SECONDS * 1000,
                                                "model_id": MUSIC_MODEL, "force_instrumental": True}, 600))
        cut = sounds.trim_intro(out)
        info = sounds.analyze_music(sounds.load_mono(out))
        sounds.upsert(folder / "library.json", "tracks", {
            "file": f"{mood}/ai_{mid}.mp3", "mood": mood, "kind": "generated", "prompt": full,
            "model": f"elevenlabs:{MUSIC_MODEL}", "bpm": info["bpm"], "energy_db": info["energy_db"],
            "lufs": sounds.lufs(out), "duration": sounds.duration(out), "intro_trimmed_s": cut, "added": sounds.now()})
        print(f"MADE music/{mood}/ai_{mid}.mp3  ({info['bpm']} BPM)", flush=True)
        made += 1
    return made


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate music and sound effects with ElevenLabs")
    ap.add_argument("--sfx", action="store_true")
    ap.add_argument("--music", action="store_true")
    ap.add_argument("--only", help="comma-separated ids from --list")
    ap.add_argument("--force", action="store_true", help="regenerate files that already exist")
    ap.add_argument("--check", action="store_true", help="check the key and credits, generate nothing")
    ap.add_argument("--list", action="store_true", help="print the catalogue")
    args = ap.parse_args()
    if args.list:
        for sid, cat, prompt, secs in SFX:
            print(f"sfx    {sid:20s} {cat:11s} {secs:>4}s  {prompt}")
        for mid, mood, prompt in MUSIC:
            print(f"music  {mid:20s} {mood:11s} {MUSIC_SECONDS:>4}s  {prompt}")
        return 0
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        print("no ElevenLabs API key: add it in Setup → ElevenLabs", file=sys.stderr)
        return 2
    ids = set(args.only.split(",")) if args.only else None
    try:
        print(check(key), flush=True)
        if args.check:
            return 0
        made = 0
        if args.sfx:
            made += make_sfx(key, _env.ROOT / "sfx", ids, args.force)
        if args.music:
            made += make_music(key, _env.ROOT / "music", ids, args.force)
        print(f"done: {made} new file(s)", flush=True)
        return 0
    except (ApiError, requests.RequestException) as e:
        print(f"stopped: {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
