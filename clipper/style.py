"""The Short "style": selection rules, hook, music and captions, shared by the engine and the renderer.

The app writes <workspace>/style.json from its Style tab. Nothing caches it: run_queue.py reads it
for every video (the selection rules and instructions go into Claude's prompt) and render_short.py
for every render. A clip spec can override any of it in its own "style" block, so the order is

    DEFAULTS  <  style.json (or render_short.py --style FILE)  <  spec["style"]

The defaults copy the reference Short the look was modelled on: a 3 s cold open of the payoff, a
neon-green hook line typed on mid-frame with a glow and no box, a ducked phonk-style beat under
the voice, and heavy white uppercase captions at the same height with a coloured accent word.

Colours are "#RRGGBB" here and in the app; ass_color() turns them into libass's &HAABBGGRR.

Keys (defaults in style_defaults.json):
    clip      minScore, maxShorts, minSeconds, maxSeconds (cold open included)
    hook      text (show spec.hook_text), seconds (on screen from 0), font, size (px on 1080x1920),
              color, box + boxOpacity (0-100; 0 = no box), glow (halo in the text colour),
              position top|middle|bottom, animation type|fade|none, coldOpen auto|always|off
              (auto: Claude decides), coldOpenSeconds, flash (white flash on the cut into the clip)
    music     enabled, folder (relative to the workspace), volume (vs the voice, in pauses), duck
    captions  enabled, font, size, color, outline, outlineWidth, shadow, highlight,
              highlightMode active (the spoken word)|keywords (spec.emphasis)|off,
              position (% of the height, from the bottom), uppercase, wordsPerCaption 1-3,
              reveal (words appear as they are spoken)
    instructions  free text for Claude
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

# The defaults live in style_defaults.json so the app's Style tab shows the same values.
DEFAULTS: dict = json.loads((Path(__file__).with_name("style_defaults.json")).read_text(encoding="utf-8"))

MOODS = ("funny", "hype", "romantic", "chill")
HOOK_POSITIONS = ("top", "middle", "bottom")
HOOK_ANIMATIONS = ("type", "fade", "none")
COLD_OPEN = ("auto", "always", "off")
HIGHLIGHT_MODES = ("active", "keywords", "off")
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

# key -> (min, max) for numbers; anything else falls back to the default's type.
RANGES = {
    ("clip", "minScore"): (0, 10), ("clip", "maxShorts"): (1, 50),
    ("clip", "minSeconds"): (5, 180), ("clip", "maxSeconds"): (5, 180),
    ("hook", "seconds"): (0.5, 10), ("hook", "size"): (20, 200), ("hook", "boxOpacity"): (0, 100),
    ("hook", "coldOpenSeconds"): (0.5, 5),
    ("music", "volume"): (0, 1),
    ("captions", "size"): (20, 200), ("captions", "outlineWidth"): (0, 20), ("captions", "shadow"): (0, 20),
    ("captions", "position"): (5, 95), ("captions", "wordsPerCaption"): (1, 3),
}
CHOICES = {
    ("hook", "position"): HOOK_POSITIONS, ("hook", "animation"): HOOK_ANIMATIONS,
    ("hook", "coldOpen"): COLD_OPEN, ("captions", "highlightMode"): HIGHLIGHT_MODES,
}


def merge(base: dict, over: dict | None) -> dict:
    """Deep merge: `over` wins key by key; nested dicts merge instead of replacing."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def clean(style: dict, fallback: dict | None = None) -> dict:
    """Coerce a merged style to valid values. Anything malformed takes the value from `fallback`
    (the already-clean layer underneath), or from DEFAULTS."""
    out = copy.deepcopy(style)
    fallback = fallback or DEFAULTS
    for section, defaults in DEFAULTS.items():
        if not isinstance(defaults, dict):
            if not isinstance(out.get(section), type(defaults)):
                out[section] = fallback.get(section, defaults)
            continue
        sec = out.get(section) if isinstance(out.get(section), dict) else {}
        for key, default in defaults.items():
            dv = fallback.get(section, {}).get(key, default)
            v = sec.get(key, dv)
            if isinstance(default, bool):
                v = v if isinstance(v, bool) else dv
            elif isinstance(default, (int, float)):
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    v = dv
                lo, hi = RANGES.get((section, key), (float("-inf"), float("inf")))
                v = min(max(v, lo), hi)
                if isinstance(default, int):
                    v = int(round(v))
            elif (section, key) in CHOICES:
                v = v if v in CHOICES[(section, key)] else dv
            elif key in ("color", "box", "outline", "highlight"):
                v = v if isinstance(v, str) and HEX.match(v) else dv
            elif not isinstance(v, str) or not v.strip():
                v = dv
            sec[key] = v
        out[section] = sec
    clip = out["clip"]
    if clip["maxSeconds"] < clip["minSeconds"]:
        clip["maxSeconds"] = clip["minSeconds"]
    return out


def read_file(path: Path | None) -> dict:
    if not path or not Path(path).exists():
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(root: Path, style_file: Path | None = None) -> dict:
    """DEFAULTS < <root>/style.json (or style_file, for previews)."""
    return clean(merge(DEFAULTS, read_file(style_file or Path(root) / "style.json")))


def resolve(base: dict, spec_style: dict | None) -> dict:
    """base (already loaded) < the clip spec's own "style" block."""
    return clean(merge(base, spec_style if isinstance(spec_style, dict) else {}), base)


def ass_color(hex_rgb: str, opacity: float = 100) -> str:
    """'#RRGGBB' + opacity 0-100 -> '&HAABBGGRR' (libass: alpha 00 = opaque, FF = invisible)."""
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    alpha = round(255 * (1 - max(0.0, min(100.0, opacity)) / 100))
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def settings_line(style: dict, instructions: str = "") -> str:
    """The user's settings, in plain words, for the headless Claude prompt."""
    c, h, m = style["clip"], style["hook"], style["music"]
    parts = [
        f"minimum score {c['minScore']:g}",
        f"at most {c['maxShorts']} Shorts",
        f"clip length {c['minSeconds']:g}-{c['maxSeconds']:g} s",
        f"hook text banner {'on: write hook_text' if h['text'] else 'off: no hook_text'}",
        {"auto": f"cold open when a clear payoff exists (cold_open about {h['coldOpenSeconds']:g} s)",
         "always": f"cold open on every clip (cold_open about {h['coldOpenSeconds']:g} s)",
         "off": "no cold open"}[h["coldOpen"]],
        "background music on: pick music_mood" if m["enabled"] else "no background music",
    ]
    if style["captions"]["highlightMode"] == "keywords":
        parts.append("caption highlight on keywords: list emphasis words")
    line = f" User settings for this run: {', '.join(parts)}."
    notes = [t.strip() for t in (style.get("instructions") or "", instructions or "") if t and t.strip()]
    if notes:
        line += (" The user's instructions (follow them; put per-clip look changes in the spec's"
                 " \"style\" block): " + " / ".join(f"\"{n}\"" for n in notes) + ".")
    return line
