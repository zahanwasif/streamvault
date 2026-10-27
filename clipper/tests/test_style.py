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
    off = styles.clean(styles.merge(styles.DEFAULTS, {"hook": {"coldOpen": "off", "text": False},
                                                     "music": {"enabled": False}}))
    line = styles.settings_line(off)
    assert "no cold open" in line and "no background music" in line and "instructions" not in line


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
    graph = rs.music_filter(30.0, -12.5, True, 0.03)
    assert "sidechaincompress=threshold=0.03000" in graph and "[mus][key]" in graph
    assert "volume=-12.50dB" in graph and "afade=t=out:st=28.500:d=1.5" in graph
    assert graph.endswith("normalize=0[aout]")
    flat = rs.music_filter(30.0, -12.5, False, 0.03)
    assert "sidechaincompress" not in flat and "[voice][bed]amix" in flat


def test_no_music_note():
    st = styles.resolve(styles.clean(styles.DEFAULTS), {"music": {"folder": "no-such-folder-here"}})
    plan, note = rs.plan_music(Path("base.mp4"), st, "funny", Path("01_x.mp4"), 30)
    assert plan is None and note.startswith("no music")
    off = styles.resolve(st, {"music": {"enabled": False}})
    assert rs.plan_music(Path("base.mp4"), off, "funny", Path("01_x.mp4"), 30) == (None, None)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
