"""Unit checks for style.py and the ASS / music parts of render_short.py.

    uv run --project video-use python clipper/tests/test_style.py
(or under pytest). Nothing here runs ffmpeg.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _env  # noqa: E402,F401
import render_short as rs  # noqa: E402
import style as styles  # noqa: E402

KEY = "src"


def words(*items: tuple[str, float, float]) -> list[dict]:
    return [{"text": t, "start": a, "end": b, "type": "word"} for t, a, b in items]


def make_edit_dir(tmp: Path) -> Path:
    edit = tmp / "edit"
    (edit / "transcripts").mkdir(parents=True)
    tr = {"words": words(("Who", 10.0, 10.3), ("am", 10.3, 10.5), ("I?", 10.5, 10.9),
                         ("You", 11.6, 11.8), ("never", 11.8, 12.2), ("watch,", 12.2, 12.6),
                         ("payoff", 30.0, 30.5), ("here", 30.5, 31.0))}
    (edit / "transcripts" / f"{KEY}.json").write_text(json.dumps(tr), encoding="utf-8")
    return edit


def edl(ranges: list[dict]) -> dict:
    return {"sources": {KEY: "x.mp4"}, "ranges": [{"source": KEY, **r} for r in ranges]}


def events(ass: str) -> list[str]:
    return [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]


def secs(t: str) -> float:
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


# ---------------------------------------------------------------- style.py

def test_color_conversion():
    assert styles.ass_color("#FFE14D") == "&H004DE1FF"
    assert styles.ass_color("#000000", 0) == "&HFF000000"
    assert styles.ass_color("#102030", 50) == "&H80302010"
    assert rs.ass_rgb("#FFE14D") == "&H4DE1FF&"


def test_merge_order_and_clean():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "style.json").write_text(json.dumps({
            "clip": {"minScore": 8}, "captions": {"color": "#FF0000", "size": 80, "highlightMode": "bogus"}}))
        base = styles.load(root)
        assert base["clip"]["minScore"] == 8                       # file beats defaults
        assert base["clip"]["maxShorts"] == styles.DEFAULTS["clip"]["maxShorts"]
        assert base["captions"]["highlightMode"] == styles.DEFAULTS["captions"]["highlightMode"]
        st = styles.resolve(base, {"captions": {"size": 90, "color": "red"}})
        assert st["captions"]["size"] == 90                        # spec beats file
        assert st["captions"]["color"] == "#FF0000"                 # malformed colour keeps the file's
        alt = root / "preview.json"
        alt.write_text(json.dumps({"clip": {"minScore": 5}}))
        assert styles.load(root, alt)["clip"]["minScore"] == 5      # --style replaces style.json
        clamp = styles.clean(styles.merge(styles.DEFAULTS, {"captions": {"wordsPerCaption": 9},
                                                            "clip": {"minSeconds": 50, "maxSeconds": 30}}))
        assert clamp["captions"]["wordsPerCaption"] == 3
        assert clamp["clip"]["maxSeconds"] == 50


def test_settings_line():
    st = styles.clean(styles.merge(styles.DEFAULTS, {"instructions": "sarcastic hooks"}))
    line = styles.settings_line(st, "only the pizza part")
    assert "minimum score 9" in line and "at most 3 Shorts" in line and "20-45 s" in line
    assert '"sarcastic hooks" / "only the pizza part"' in line
    assert "You decide the look of every clip" in line                    # the look is Claude's call
    line = styles.settings_line(styles.clean(styles.DEFAULTS))
    assert "instructions:" not in line and "skill's defaults" in line


# ---------------------------------------------------------------- ranges

def test_cold_open_goes_first():
    st = styles.clean(styles.DEFAULTS)
    spec = {"ranges": [{"start": 10, "end": 20}, {"start": 25, "end": 31}], "cold_open": {"start": 29, "end": 31}}
    ranges, cold = rs.edit_ranges(spec, st)
    assert ranges[0] == {"start": 29.0, "end": 31.0} and len(ranges) == 3 and cold == 2.0
    off = styles.resolve(st, {"hook": {"coldOpen": "off"}})
    assert rs.edit_ranges(spec, off) == ([{"start": 10.0, "end": 20.0}, {"start": 25.0, "end": 31.0}], 0.0)
    always = styles.resolve(st, {"hook": {"coldOpen": "always", "coldOpenSeconds": 2}})
    ranges, cold = rs.edit_ranges({"ranges": spec["ranges"]}, always)
    assert ranges[0] == {"start": 29.0, "end": 31.0} and cold == 2.0


# ---------------------------------------------------------------- ASS

def build(tmp: Path, spec: dict, over: dict, ranges=None, cold=0.0) -> str:
    st = styles.resolve(styles.clean(styles.DEFAULTS), over)
    e = edl(ranges or [{"start": 10.0, "end": 13.0}])
    dur = sum(r["end"] - r["start"] for r in e["ranges"])
    return rs.build_ass(e, make_edit_dir(tmp), st, spec, dur, cold, True)


def test_highlight_modes():
    plain = {"hook": {"text": False}, "captions": {"reveal": False}}
    with tempfile.TemporaryDirectory() as d:
        ass = build(Path(d), {}, styles.merge(plain, {"captions": {"highlightMode": "off"}}))
        ev = events(ass)
        assert len(ev) == 2                                        # 2 chunks, one event each
        assert "WHO" in ev[0] and "&H4CFF4C&" not in ass.split("[Events]")[1]
    with tempfile.TemporaryDirectory() as d:
        ass = build(Path(d), {}, styles.merge(plain, {"captions": {"highlightMode": "active", "highlight": "#FFE14D"}}))
        ev = events(ass)
        assert len(ev) == 6                                        # one per word
        assert ev[0].count("&H4DE1FF&") == 1 and re.search(r"\\1c&H4DE1FF&\\alpha&H00&\}WHO", ev[0])
        assert re.search(r"\\1c&H4DE1FF&\\alpha&H00&\}AM", ev[1])
    with tempfile.TemporaryDirectory() as d:
        ass = build(Path(d), {"emphasis": ["Never"]}, styles.merge(plain, {"captions": {"highlightMode": "keywords"}}))
        ev = events(ass)
        assert len(ev) == 2
        assert re.search(r"&H4CFF4C&\\alpha&H00&\}NEVER", ev[1]) and ev[0].count("&H4CFF4C&") == 0
        assert "WATCH," not in ev[1] and "WATCH" in ev[1]          # trailing comma stripped
    with tempfile.TemporaryDirectory() as d:  # reveal: later words of the cue are drawn invisible
        ass = build(Path(d), {}, {"hook": {"text": False}, "captions": {"highlightMode": "off", "reveal": True}})
        ev = events(ass)
        assert len(ev) == 6 and "\\alpha&HFF&}AM" in ev[0] and "\\alpha&H00&}AM" in ev[1]
    with tempfile.TemporaryDirectory() as d:  # wordsPerCaption 1
        ass = build(Path(d), {}, styles.merge(plain, {"captions": {"wordsPerCaption": 1, "highlightMode": "off"}}))
        assert len(events(ass)) == 6


def test_caption_times_follow_ranges():
    with tempfile.TemporaryDirectory() as d:
        ass = build(Path(d), {}, {"hook": {"text": False}, "captions": {"reveal": False, "highlightMode": "off"}},
                    ranges=[{"start": 30.0, "end": 31.2}, {"start": 10.0, "end": 13.0}], cold=1.2)
        ev = events(ass)
        assert "PAYOFF" in ev[0] and ev[0].split(",")[1] == "0:00:00.00"
        who = next(e for e in ev if "WHO" in e)
        assert abs(secs(who.split(",")[1]) - 1.2) < 0.02              # shifted by the cold open
        flash = [e for e in ev if ",Flash," in e]
        assert len(flash) == 1 and flash[0].split(",")[1] == "0:00:01.20"


def test_hook_events():
    with tempfile.TemporaryDirectory() as d:
        ass = build(Path(d), {"hook_text": "He did not"}, {"captions": {"enabled": False}})
        hook = [e for e in events(ass) if ",Hook," in e]
        crisp = [e for e in hook if e.startswith("Dialogue: 2")]
        glow = [e for e in hook if e.startswith("Dialogue: 1")]
        assert len(crisp) == len(glow) == len("HEDIDNOT")         # one per typed character
        assert crisp[0].split(",")[1] == "0:00:00.00"
        assert crisp[-1].split(",")[2] == "0:00:03.00"             # clip is 3 s, shorter than hook.seconds
        assert "\\fad(0,250)" in crisp[-1] and "{\\alpha&HFF&}E DID NOT" in crisp[0]
        assert "\\blur" in glow[0] and "\\blur" not in crisp[0]
        assert "Style: Hook,Arial Black,62,&H004CFF4C" in ass
    with tempfile.TemporaryDirectory() as d:  # box, fade, top; captions are not hidden then
        ass = build(Path(d), {"hook_text": "Wait"}, {"hook": {"boxOpacity": 60, "animation": "fade",
                                                              "position": "top", "seconds": 2}})
        hook = [e for e in events(ass) if ",Hook," in e]
        assert len(hook) == 1 and "\\fad(200,250)" in hook[0] and hook[0].split(",")[2] == "0:00:02.00"
        assert re.search(r"Style: Hook,.*,&H66000000,&H00000000,0,0,0,0,100,100,0,0,3,18,0,8,", ass)
        assert any("WHO" in e for e in events(ass))
    with tempfile.TemporaryDirectory() as d:  # middle hook hides captions that start under it
        ass = build(Path(d), {"hook_text": "Wait"}, {"hook": {"seconds": 1.5}},
                    ranges=[{"start": 10.0, "end": 13.0}])
        caps = [e for e in events(ass) if ",Caption," in e]
        assert caps and all(secs(e.split(",")[1]) >= 1.5 for e in caps)
    with tempfile.TemporaryDirectory() as d:
        ass = build(Path(d), {"hook_text": "Wait"}, {"hook": {"text": False}})
        assert not [e for e in events(ass) if ",Hook," in e]


# ---------------------------------------------------------------- music

def test_music_tracks_and_graph():
    st = styles.clean(styles.DEFAULTS)
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        assert rs.find_tracks(root, st, "funny") == ([], "music/")
        (root / "music" / "funny").mkdir(parents=True)
        (root / "music" / "hype").mkdir()
        assert rs.find_tracks(root, st, "funny")[0] == []
        for name in ("a.mp3", "b.mp3", "c.m4a", "notes.txt"):
            (root / "music" / "hype" / name).write_bytes(b"")
        tracks, where = rs.find_tracks(root, st, "funny")      # nothing for funny: any mood folder
        assert [t.name for t in tracks] == ["a.mp3", "b.mp3", "c.m4a"] and where == "music/*/"
        (root / "music" / "funny" / "f.mp3").write_bytes(b"")
        assert [t.name for t in rs.find_tracks(root, st, "funny")[0]] == ["f.mp3"]
        out = root / "shorts" / "slug"
        picks = [rs.pick_track(tracks, out / f"{n:02d}_funny_x.mp4").name for n in (1, 2, 3, 4)]
        assert picks[0] == rs.pick_track(tracks, out / "01_funny_x.mp4").name   # deterministic
        assert len(set(picks[:3])) == 3 and picks[3] == picks[0]                  # rotates
    music = {"gain_db": -12.5, "duck": True, "threshold": 0.03}
    graph = rs.audio_graph(30.0, music)
    assert "[1:a]" in graph and "[mus][vkey]sidechaincompress=threshold=0.03000" in graph
    assert "volume=-12.50dB" in graph and "afade=t=out:st=28.500:d=1.5" in graph
    assert graph.endswith("normalize=0[aout]")
    flat = rs.audio_graph(30.0, dict(music, duck=False))
    assert "sidechaincompress" not in flat and "[clip][bed]amix" in flat
    looped = rs.audio_graph(30.0, music, loop=True)
    assert "afade=t=out:st=29.850:d=0.15" in looped                      # loop ending: no long fade


def test_no_music_note():
    st = styles.resolve(styles.clean(styles.DEFAULTS), {"music": {"folder": "no-such-folder-here"}})
    plan, note = rs.plan_music(Path("base.mp4"), st, "funny", Path("01_x.mp4"))
    assert plan is None and note.startswith("no music")
    off = styles.resolve(st, {"music": {"enabled": False}})
    assert rs.plan_music(Path("base.mp4"), off, "funny", Path("01_x.mp4")) == (None, None)


# ---------------------------------------------------------------- tracking, zoom, sfx, voice

def test_tracking_path_and_coverage():
    import track
    # The face walks from x=0.2 to x=0.7 over 4 s, then stays: the crop follows, inside the frame.
    samples = [(i / 5, (min(0.7, 0.2 + i * 0.025) - 0.03, 0.3, 0.06, 0.1)) for i in range(40)]
    keys = track.smooth_path(samples, 0.316)
    assert keys[0][1] == 0.2 and abs(keys[-1][1] - 0.7) < 0.06
    assert all(0.158 <= cx <= 0.842 for _, cx in keys)
    speeds = [abs(b - a) / (t1 - t0) for (t0, a), (t1, b) in zip(keys, keys[1:]) if t1 > t0]
    assert max(speeds) <= track.MAX_SPEED + 1e-6                         # never whips across the frame
    tr = track.Track(samples=samples, keyframes=keys, crop_w=0.316)
    assert track.coverage(tr, {"type": "track"}) > 0.9
    assert track.coverage(tr, {"type": "center", "cx": 0.2}) < 0.5        # a fixed crop loses the subject
    assert track.coverage(tr, {"type": "blur"}) == 1.0
    assert abs(track.cx_at(keys, 0.0) - 0.2) < 1e-6
    still = track.smooth_path([(i / 5, (0.48, 0.3, 0.06, 0.1)) for i in range(10)], 0.316)
    assert len(still) == 2                                               # dead zone: no jitter keyframes


def test_crop_and_zoom_expressions():
    import track
    x = track.x_expr([(0.0, 0.3), (2.0, 0.6)])
    assert x.startswith("max(0,min(iw-ow,(") and "if(lt(t,2.000),0.3000+(0.15000)*(t-0.000),0.6000)" in x
    assert rs.layout_filter({"type": "track"}, x).startswith(f"crop=w=ih*9/16:h=ih:x='{x}'")
    assert "x='max(0,min(iw-ow,0.4*iw-ow/2))'" in rs.layout_filter({"type": "center", "cx": 0.4})
    z = rs.zoom_filter([(1.0, 0.6, 1.2)], 0.3, "60/1")
    assert z.startswith("zoompan=z='1+0.200*if(between(it,1.000,1.080)") and ":y='(ih-ih/zoom)*0.300'" in z
    assert z.endswith("s=1080x1920:fps=60/1")
    st = styles.clean(styles.DEFAULTS)
    spec = {"zooms": [{"at": 12.0, "scale": 9}, {"at": 50}]}
    assert rs.segment_zooms(spec, st, {"start": 10.0, "end": 20.0}, 1, 0.0) == [(2.0, 0.6, 1.6)]   # clamped
    assert rs.segment_zooms({}, st, {"start": 30.0, "end": 33.0}, 0, 3.0) == [(0.0, 0.6, 1.15)]   # cold open
    # Written as {start, end} (a common slip) it still works.
    assert rs.segment_zooms({"zooms": [{"start": 12.0, "end": 13.6}]}, st, {"start": 10.0, "end": 20.0}, 1, 0.0) \
        == [(2.0, 1.6, 1.15)]
    off = styles.resolve(st, {"zoom": {"enabled": False}})
    assert rs.segment_zooms(spec, off, {"start": 10.0, "end": 20.0}, 1, 0.0) == []


def test_sfx_plan_and_graph():
    ranges = [{"start": 30.0, "end": 33.0}, {"start": 10.0, "end": 20.0}, {"start": 25.0, "end": 35.0}]
    assert rs.output_times(ranges, 31.0) == [1.0, 19.0]                  # the payoff plays twice
    with tempfile.TemporaryDirectory() as d:
        folder = Path(d) / "sfx"
        for rel in ("transition/whoosh_1.mp3", "transition/riser_1.mp3", "impact/boom_1.mp3"):
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_bytes(b"x")
        (folder / "library.json").write_text(json.dumps({"sounds": [
            {"file": "transition/riser_1.mp3", "category": "transition", "tags": ["riser"], "hit_ms": 2500, "lufs": -16}]}))
        lib = rs.sfx_library(folder)
        assert {e["file"] for e in lib} == {"transition/whoosh_1.mp3", "transition/riser_1.mp3", "impact/boom_1.mp3"}
        assert rs.resolve_sfx(lib, "impact/boom", 0)["file"] == "impact/boom_1.mp3"
        assert rs.resolve_sfx(lib, "whoosh", 3)["file"] == "transition/whoosh_1.mp3"
        assert rs.resolve_sfx(lib, "impact", 0)["file"] == "impact/boom_1.mp3"     # a category alone
        assert rs.resolve_sfx(lib, "laugh", 0) is None
        st = styles.resolve(styles.clean(styles.DEFAULTS), {"sfx": {"folder": str(folder), "volume": 1.0}})
        spec = {"cold_open": {"start": 30.0, "end": 33.0}, "sfx": [{"at": 31.0, "sound": "impact/boom"},
                                                                   {"at": 12.0, "sound": "crowd/laugh"}]}
        plan, notes = rs.plan_sfx(spec, st, ranges, 3.0, Path("01_x.mp4"))
        sounds = sorted((e["sound"], round(e["at"], 2)) for e in plan)
        assert ("impact/boom_1.mp3", 1.0) in sounds and ("impact/boom_1.mp3", 19.0) in sounds
        assert ("transition/whoosh_1.mp3", 3.0) in sounds                  # the cut out of the cold open
        assert ("transition/whoosh_1.mp3", 13.0) in sounds                 # a jump cut, 10 s later
        riser = next(e for e in plan if e["sound"] == "transition/riser_1.mp3")
        assert round(riser["at"], 2) == 15.5 and round(riser["gain_db"], 2) == -7.0   # peak lands on the payoff, 18 s
        assert notes == ["no sound effect for crowd/laugh"]
        plan, _ = rs.plan_sfx({"sfx": [{"at": 12.0, "file": "impact/boom_1.mp3"}]},          # "file" for "sound"
                              styles.resolve(st, {"sfx": {"autoCuts": False}}), ranges, 3.0, Path("01_x.mp4"))
        assert [(e["sound"], round(e["at"], 2)) for e in plan] == [("impact/boom_1.mp3", 5.0)]
    effects = [{"file": "w.mp3", "at": 3.0, "gain_db": -2.0}, {"file": "r.mp3", "at": -0.5, "gain_db": 0.0}]
    g = rs.audio_graph(30.0, {"gain_db": -10, "duck": True, "threshold": 0.03}, {"file": "v.wav", "volume": 1.0}, effects)
    assert "[2:a]" in g and "[3:a]" in g and "[4:a]" in g                  # music 1, narration 2, effects 3-4
    assert "[clip][narkey]sidechaincompress" in g                         # the clip ducks under the narration
    assert "[voice]asplit=2[vmix][vkey]" in g and "[mus][vkey]" in g        # music keys on voice only
    assert "adelay=3000|3000,volume=-2.00dB[s0]" in g and "atrim=start=0.500" in g
    assert "[vmix][s0][s1]amix=inputs=3" in g


def test_sound_catalogue_and_levels():
    import sound_library
    assert all(sound_library.SFX_MIN_S <= secs <= sound_library.SFX_MAX_S for *_, secs in sound_library.SFX)
    assert len({sid for sid, *_ in sound_library.SFX}) == len(sound_library.SFX)
    assert len({mid for mid, *_ in sound_library.MUSIC}) == len(sound_library.MUSIC)
    with tempfile.TemporaryDirectory() as d:
        folder = Path(d) / "sfx"
        (folder / "impact").mkdir(parents=True)
        (folder / "impact" / "thud_1.mp3").write_bytes(b"x")
        (folder / "library.json").write_text(json.dumps({"sounds": [
            {"file": "impact/thud_1.mp3", "category": "impact", "tags": ["thud"], "hit_ms": 0, "lufs": -39.7}]}))
        st = styles.resolve(styles.clean(styles.DEFAULTS), {"sfx": {"folder": str(folder), "volume": 1.0, "autoCuts": False}})
        plan, _ = rs.plan_sfx({"sfx": [{"at": 1.0, "sound": "impact/thud"}]}, st, [{"start": 0.0, "end": 5.0}], 0.0,
                              Path("01_x.mp4"))
        assert round(plan[0]["gain_db"], 2) == 12.0                       # a near-silent file isn't lifted 20 dB


def test_voice_hook():
    st = styles.clean(styles.DEFAULTS)
    assert rs.voice_line({"voice_hook": True, "hook_text": "He had no idea"}, st) == "He had no idea"
    assert rs.voice_line({"voice_hook": "  Wait for it  "}, st) == "Wait for it"
    assert rs.voice_line({}, st) is None
    assert rs.voice_line({"voice_hook": "x"}, styles.resolve(st, {"hook": {"voice": False}})) is None
    import os
    saved = os.environ.get("VOICESTUDIO_URL")
    os.environ["VOICESTUDIO_URL"] = "http://127.0.0.1:9"                 # nothing listens there
    rs._personalities = None
    try:
        with tempfile.TemporaryDirectory() as d:
            wav, note = rs.synth_voice("hello", st, Path(d))
        assert wav is None and note.startswith("voice hook skipped")
    finally:
        rs._personalities = None
        if saved is None:
            os.environ.pop("VOICESTUDIO_URL", None)
        else:
            os.environ["VOICESTUDIO_URL"] = saved


def test_captions_never_overlap():
    # "I?" ends a cue early, then "YOU" starts 0.7 s later; a too-short cue stretched to 0.4 s must
    # still end before the next one starts, or libass stacks the two on screen.
    with tempfile.TemporaryDirectory() as d:
        for reveal in (True, False):
            ass = build(Path(d) / str(reveal), {}, {"hook": {"text": False}, "captions": {"reveal": reveal, "wordsPerCaption": 1,
                                                                                    "highlightMode": "off"}})
            spans = sorted((secs(e.split(",")[1]), secs(e.split(",")[2])) for e in events(ass))
            assert all(b <= c + 1e-6 for (_, b), (c, _) in zip(spans, spans[1:]))


def test_loop_ending_captions():
    with tempfile.TemporaryDirectory() as d:
        ass = build(Path(d), {}, {"hook": {"text": False, "loop": True}, "captions": {"reveal": False, "highlightMode": "off"}},
                    ranges=[{"start": 10.0, "end": 12.7}])
        ends = [secs(e.split(",")[2]) for e in events(ass)]
        assert max(ends) <= 2.45 + 0.01                                    # nothing in the last 0.25 s


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
