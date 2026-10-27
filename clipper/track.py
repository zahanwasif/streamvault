"""Follow the main subject's face through a time range and turn it into a smooth 9:16 crop path.

render_short.py uses this for the `track` layout (the crop follows the speaker) and to check that the
subject is actually in frame for `center`, `track` and `split` layouts. Claude uses the CLI to look
at a clip before picking its layout: the PNG is a filmstrip with the crop window and the faces drawn.

Faces come from OpenCV's YuNet detector (clipper/models/face_detection_yunet_2023mar.onnx, MIT).
Without OpenCV, available() is False and callers fall back to a fixed crop.

Coordinates are fractions (0..1) of the source frame; times are seconds from the range start.

Usage:
    python clipper/track.py <video> <start> <end> [--layout '{"type":"track"}'] [--png out.png]
prints JSON: {"keyframes": [[t, cx], ...], "coverage": 0.93, "detected": 0.88, "face_y": [0.21, 0.45]}
"""

from __future__ import annotations

import _env  # noqa: F401  (must come first: sys.path, PATH, UTF-8)

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import cv2
    import numpy as np
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)   # its DNN backend warns on every load
except ImportError:  # the renderer still works, with a fixed crop
    cv2 = None
    np = None

MODEL = Path(__file__).resolve().parent / "models" / "face_detection_yunet_2023mar.onnx"
SAMPLE_FPS = 5
DETECT_W = 640
CROP_W = 9 / 16 / (16 / 9)    # a full-height 9:16 window over a 16:9 frame: 0.316 of the width
DEADZONE = 0.05               # the crop doesn't move while the face stays within this of its centre
MAX_SPEED = 0.35              # fraction of the frame width per second
EASE = 0.35                   # how quickly the crop catches up, per sample
LOST_S = 1.0                  # after this long without the subject, take the biggest face again


def available() -> bool:
    return cv2 is not None and MODEL.exists()


@dataclass
class Track:
    samples: list[tuple[float, tuple[float, float, float, float] | None]]  # (t, subject box x,y,w,h)
    keyframes: list[tuple[float, float]] = field(default_factory=list)    # (t, crop centre x)
    crop_w: float = CROP_W

    @property
    def detected(self) -> float:
        return sum(b is not None for _, b in self.samples) / max(1, len(self.samples))

    def face_y(self) -> tuple[float, float] | None:
        """Median top and bottom of the subject's face, as fractions of the frame height."""
        boxes = [b for _, b in self.samples if b]
        if not boxes:
            return None
        tops = sorted(b[1] for b in boxes)
        bottoms = sorted(b[1] + b[3] for b in boxes)
        return tops[len(tops) // 2], bottoms[len(bottoms) // 2]

    def face_cy(self) -> float:
        fy = self.face_y()
        return (fy[0] + fy[1]) / 2 if fy else 0.5


# ---------------------------------------------------------------- detection

def frame_size(video: Path) -> tuple[int, int]:
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "json", str(video)], capture_output=True, text=True, check=True)
    s = json.loads(probe.stdout)["streams"][0]
    return int(s["width"]), int(s["height"])


def crop_width(video: Path) -> float:
    """Width of a full-height 9:16 window, as a fraction of the source width."""
    w, h = frame_size(video)
    return min(1.0, h * 9 / 16 / w)


def read_frames(video: Path, start: float, end: float, fps: int = SAMPLE_FPS, width: int = DETECT_W):
    """Yield (t, BGR frame) at `fps` from start to end, via ffmpeg (handles every codec the renderer does)."""
    sw, sh = frame_size(video)
    h = int(round(sh * width / sw / 2) * 2)
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", str(video), "-t", f"{max(0.1, end - start):.3f}",
           "-vf", f"fps={fps},scale={width}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = width * h * 3
    i = 0
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield i / fps, np.frombuffer(buf, np.uint8).reshape(h, width, 3)
            i += 1
    finally:
        p.stdout.close()
        p.wait()


def detect(video: Path, start: float, end: float) -> list[tuple[float, list[tuple[float, float, float, float, float]]]]:
    """[(t, [(x, y, w, h, score), ...])] with boxes as fractions of the frame."""
    out = []
    detector = None
    for t, frame in read_frames(video, start, end):
        fh, fw = frame.shape[:2]
        if detector is None:
            detector = cv2.FaceDetectorYN.create(str(MODEL), "", (fw, fh), 0.6, 0.3, 50)
        _, faces = detector.detect(frame)
        boxes = [] if faces is None else [
            (float(f[0]) / fw, float(f[1]) / fh, float(f[2]) / fw, float(f[3]) / fh, float(f[14])) for f in faces]
        out.append((t, boxes))
    return out


# ---------------------------------------------------------------- subject + path

def pick_subject(frames: list[tuple[float, list]]) -> list[tuple[float, tuple | None]]:
    """Follow one face: start with the biggest, then the nearest similar-sized one each frame."""
    chosen: list[tuple[float, tuple | None]] = []
    last, last_t = None, -1e9
    for t, boxes in frames:
        pick = None
        if boxes:
            if last is not None and t - last_t <= LOST_S:
                lx, ly = last[0] + last[2] / 2, last[1] + last[3] / 2

                def cost(b):
                    dx, dy = b[0] + b[2] / 2 - lx, b[1] + b[3] / 2 - ly
                    return (dx * dx + dy * dy) ** 0.5 + abs(b[2] - last[2]) * 2

                near = min(boxes, key=cost)
                pick = near if cost(near) < 0.25 else None
            if pick is None and (last is None or t - last_t > LOST_S):
                pick = max(boxes, key=lambda b: b[2] * b[3] * b[4])
        if pick is not None:
            last, last_t = pick, t
        chosen.append((t, tuple(pick[:4]) if pick else None))
    return chosen


def smooth_path(samples: list[tuple[float, tuple | None]], crop_w: float = CROP_W,
                start_cx: float = 0.5) -> list[tuple[float, float]]:
    """Crop-centre keyframes: hold inside a dead zone, ease toward the face, never pan faster than MAX_SPEED."""
    half = crop_w / 2
    clamp = lambda v: min(1 - half, max(half, v))  # noqa: E731
    known = [b for _, b in samples if b]
    cx = clamp(known[0][0] + known[0][2] / 2 if known else start_cx)
    path: list[tuple[float, float]] = []
    prev_t = samples[0][0] if samples else 0.0
    for t, box in samples:
        dt = max(1e-3, t - prev_t)
        prev_t = t
        if box is not None:
            target = clamp(box[0] + box[2] / 2)
            if abs(target - cx) > DEADZONE:
                step = (target - cx) * EASE
                limit = MAX_SPEED * dt
                cx = clamp(cx + max(-limit, min(limit, step)))
        path.append((t, cx))
    # Keep only the points where the direction or speed changes: a short, exact ffmpeg expression.
    keys = path[:1]
    for i in range(1, len(path) - 1):
        (t0, a), (t1, b), (t2, c) = keys[-1], path[i], path[i + 1]
        predicted = a + (c - a) * (t1 - t0) / max(1e-6, t2 - t0)
        if abs(predicted - b) > 0.004:
            keys.append(path[i])
    if len(path) > 1:
        keys.append(path[-1])
    return [(round(t, 3), round(v, 4)) for t, v in keys]


def track(video: Path, start: float, end: float, start_cx: float = 0.5) -> Track:
    samples = pick_subject(detect(video, start, end))
    cw = crop_width(video)
    return Track(samples=samples, keyframes=smooth_path(samples, cw, start_cx), crop_w=cw)


def cx_at(keyframes: list[tuple[float, float]], t: float) -> float:
    if not keyframes:
        return 0.5
    if t <= keyframes[0][0]:
        return keyframes[0][1]
    for (t0, a), (t1, b) in zip(keyframes, keyframes[1:]):
        if t <= t1:
            return a + (b - a) * (t - t0) / max(1e-6, t1 - t0)
    return keyframes[-1][1]


def coverage(tr: Track, layout: dict) -> float:
    """Share of the frames with a detected subject whose face centre is inside what the Short shows."""
    kind = layout.get("type", "blur")
    seen = [(t, b) for t, b in tr.samples if b]
    if not seen or kind in ("blur", "fit"):
        return 1.0
    inside = 0
    for t, (x, y, w, h) in seen:
        fx, fy = x + w / 2, y + h / 2
        if kind == "split":
            bx, by, bw, bh = layout["cam"]
            ok = bx <= fx <= bx + bw and by <= fy <= by + bh
            # a face in the game area also counts: the bottom half shows it
            gx, gy, gw, gh = layout.get("game", [0, 0, 1, 1])
            ok = ok or (gx <= fx <= gx + gw and gy <= fy <= gy + gh)
        else:
            cx = cx_at(tr.keyframes, t) if kind == "track" else float(layout.get("cx", 0.5))
            half = tr.crop_w / 2
            cx = min(1 - half, max(half, cx))
            ok = cx - half <= fx <= cx + half
        inside += ok
    return inside / len(seen)


def x_expr(keyframes: list[tuple[float, float]]) -> str:
    """ffmpeg crop `x` that follows the keyframes (t = seconds into the segment), clamped to the frame."""
    if not keyframes:
        cx = "0.5"
    elif len(keyframes) == 1:
        cx = f"{keyframes[0][1]:.4f}"
    else:
        cx = f"{keyframes[-1][1]:.4f}"
        for (t0, a), (t1, b) in reversed(list(zip(keyframes, keyframes[1:]))):
            slope = (b - a) / max(1e-6, t1 - t0)
            cx = f"if(lt(t,{t1:.3f}),{a:.4f}+({slope:.5f})*(t-{t0:.3f}),{cx})"
        cx = f"if(lt(t,{keyframes[0][0]:.3f}),{keyframes[0][1]:.4f},{cx})"
    return f"max(0,min(iw-ow,({cx})*iw-ow/2))"


# ---------------------------------------------------------------- filmstrip for Claude

def filmstrip(video: Path, start: float, end: float, tr: Track, layout: dict, out: Path, n: int = 8) -> Path:
    frames = list(read_frames(video, start, end, fps=max(1, round(n / max(0.5, end - start))) + 1, width=480))
    if len(frames) > n:
        frames = [frames[round(i * (len(frames) - 1) / (n - 1))] for i in range(n)]
    tiles = []
    for t, frame in frames:
        img = frame.copy()
        fh, fw = img.shape[:2]
        kind = layout.get("type", "blur")
        if kind in ("track", "center"):
            cx = cx_at(tr.keyframes, t) if kind == "track" else float(layout.get("cx", 0.5))
            half = tr.crop_w / 2
            cx = min(1 - half, max(half, cx))
            cv2.rectangle(img, (int((cx - half) * fw), 0), (int((cx + half) * fw), fh - 1), (60, 220, 60), 3)
        elif kind == "split":
            for box, col in ((layout["cam"], (60, 220, 60)), (layout.get("game", [0, 0, 1, 1]), (220, 160, 60))):
                bx, by, bw, bh = box
                cv2.rectangle(img, (int(bx * fw), int(by * fh)), (int((bx + bw) * fw), int((by + bh) * fh)), col, 2)
        box = min(tr.samples, key=lambda s: abs(s[0] - t))[1] if tr.samples else None
        if box:
            x, y, w, h = box
            cv2.rectangle(img, (int(x * fw), int(y * fh)), (int((x + w) * fw), int((y + h) * fh)), (40, 40, 240), 2)
        cv2.putText(img, f"{start + t:.1f}s", (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        tiles.append(img)
    rows = [np.hstack(tiles[i:i + 4]) for i in range(0, len(tiles), 4)]
    width = max(r.shape[1] for r in rows)
    rows = [np.pad(r, ((0, 0), (0, width - r.shape[1]), (0, 0))) for r in rows]
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), np.vstack(rows))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Follow the main face through a range; print the crop path")
    ap.add_argument("video", type=Path)
    ap.add_argument("start", type=float)
    ap.add_argument("end", type=float)
    ap.add_argument("--layout", default='{"type": "track"}', help="layout JSON, as in a clip spec")
    ap.add_argument("--png", type=Path, help="write a filmstrip with the crop window and faces")
    args = ap.parse_args()
    if not available():
        sys.exit("face tracking needs opencv-python-headless (Setup → Python environment installs it)")
    layout = json.loads(args.layout)
    tr = track(args.video, args.start, args.end, float(layout.get("cx", 0.5)))
    result = {"keyframes": tr.keyframes, "coverage": round(coverage(tr, layout), 3),
              "detected": round(tr.detected, 3), "face_y": tr.face_y()}
    if args.png:
        result["png"] = str(filmstrip(args.video, args.start, args.end, tr, layout, args.png))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
