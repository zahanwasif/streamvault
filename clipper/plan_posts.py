"""Plan YouTube posting for a folder of rendered Shorts → youtube blocks + schedule.md

Reads shorts/<slug>/posts_copy.json (per-Short copy written by Claude) and gives every Short
that hasn't been posted yet a publish slot, then writes a `youtube` block into its meta JSON
and a human-readable shorts/<slug>/schedule.md. Existing meta fields are left alone.

Slots are whole hours between --first-hour and --last-hour in --tz (US Eastern by default),
starting at least --buffer-min from now, one per hour. The highest-scored Shorts get the
--peaks hours; the rest fill the remaining slots in rank order. zoneinfo handles DST.

posts_copy.json:
{
  "creator": "IShowSpeed", "cta": "Follow for more Speed moments every day.",
  "lead_hashtags": ["#ishowspeed", "#speed", "#shorts"],
  "common_tags": ["ishowspeed", ...],
  "posts": {"02": {"title": "...", "quote": ["line", ...], "setup": "...",
                   "tags": ["..."], "hashtags": ["#iphone"]}, ...}
}

Usage:
    python clipper/plan_posts.py shorts/<slug> [--tz America/New_York] [--peaks 12,13,18,19,20]
"""

from __future__ import annotations

import _env  # noqa: F401

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PKT = ZoneInfo("Asia/Karachi")
UTC = ZoneInfo("UTC")
TITLE_MAX = 100
TAGS_MAX = 500


def build_description(post: dict, copy: dict, source_url: str) -> str:
    quote = "\n".join(f'"{q}"' for q in post["quote"])
    hashtags = " ".join(copy["lead_hashtags"] + post.get("hashtags", []))
    return (f"{quote}\n\n{post['setup']}\n\n"
            f"Clipped with permission from {copy['creator']}. Full stream: {source_url}\n\n"
            f"{copy['cta']}\n\n{hashtags}")


def build_tags(post: dict, copy: dict) -> list[str]:
    tags: list[str] = []
    for t in copy["common_tags"] + post.get("tags", []):
        if t not in tags and len(",".join(tags + [t])) <= TAGS_MAX:
            tags.append(t)
    return tags


def slots(now: datetime, n: int, tz: ZoneInfo, first: int, last: int, buffer_min: int) -> list[datetime]:
    t = (now + timedelta(minutes=buffer_min)).astimezone(tz)
    t = t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    out: list[datetime] = []
    while len(out) < n:
        if first <= t.hour <= last:
            out.append(t)
        # Step in UTC so a DST change can't repeat or skip an hour.
        t = (t.astimezone(UTC) + timedelta(hours=1)).astimezone(tz)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Plan YouTube publish slots and metadata for Shorts")
    ap.add_argument("folder", type=Path)
    ap.add_argument("--tz", default="America/New_York", help="audience time zone")
    ap.add_argument("--first-hour", type=int, default=8)
    ap.add_argument("--last-hour", type=int, default=22)
    ap.add_argument("--peaks", default="12,13,18,19,20", help="best hours, for the top-scored Shorts")
    ap.add_argument("--buffer-min", type=int, default=90, help="time to finish uploading before slot 1")
    args = ap.parse_args()

    tz = ZoneInfo(args.tz)
    peaks = {int(h) for h in args.peaks.split(",") if h}
    copy = json.loads((args.folder / "posts_copy.json").read_text(encoding="utf-8"))

    metas: list[tuple[Path, dict]] = []
    for p in sorted(args.folder.glob("[0-9][0-9]_*.json")):
        m = json.loads(p.read_text(encoding="utf-8"))
        if m.get("youtube", {}).get("studio_status") in ("published", "scheduled"):
            continue
        if p.name[:2] not in copy["posts"]:
            raise SystemExit(f"posts_copy.json has no entry for {p.name[:2]}")
        metas.append((p, m))
    if not metas:
        raise SystemExit("nothing left to plan")

    free = slots(datetime.now(UTC), len(metas), tz, args.first_hour, args.last_hour, args.buffer_min)
    by_score = sorted(metas, key=lambda pm: (-pm[1].get("score", 0), pm[0].name))
    peak_slots = [s for s in free if s.hour in peaks]
    other_slots = [s for s in free if s.hour not in peaks]
    assigned: dict[str, datetime] = {}
    for p, _ in by_score:
        pool = peak_slots if peak_slots else other_slots
        assigned[p.name] = pool.pop(0)

    rows = []
    for p, m in sorted(metas, key=lambda pm: assigned[pm[0].name]):
        post = copy["posts"][p.name[:2]]
        at = assigned[p.name]
        yt = {
            "title": post["title"],
            "description": build_description(post, copy, m["source_url"]),
            "tags": build_tags(post, copy),
            "category": "Entertainment", "language": "English",
            "made_for_kids": False, "altered_content": False,
            "visibility": "scheduled",
            "publish_at_utc": at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "publish_at_local": at.strftime(f"%Y-%m-%d %H:%M {args.tz}"),
            "publish_at_pkt": at.astimezone(PKT).strftime("%Y-%m-%d %H:%M PKT"),
            "studio_status": "pending",
        }
        assert len(yt["title"]) <= TITLE_MAX, p.name
        assert len(",".join(yt["tags"])) <= TAGS_MAX, p.name
        m["youtube"] = yt
        p.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
        rows.append(f"| {p.name[:2]} | {at:%a %b %d %I:%M %p} | {at.astimezone(PKT):%a %b %d %I:%M %p} "
                    f"| {m.get('score', '')} | {yt['title']} | {p.with_suffix('.mp4').name} | pending |")

    lines = ["# Posting schedule", "",
             f"Times in {args.tz} (audience) and Pakistan time. Status is updated as each Short is scheduled.", "",
             "| # | US Eastern | Pakistan | Score | Title | File | Status |",
             "|---|---|---|---|---|---|---|", *rows, ""]
    (args.folder / "schedule.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
