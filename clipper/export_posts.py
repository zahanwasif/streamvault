"""Hand rendered Shorts to the n8n poster: copy each one into the synced OneDrive ready/ folder.

    <OneDrive>/StreamVault/ready/<post id>/video.mp4
    <OneDrive>/StreamVault/ready/<post id>/post.json     (written last, so n8n never sees half a post)

The post id is `<export time>_<NN>_<slug>`, so n8n's "oldest first" order posts sources in the order
they were processed and, within a source, the best-scored Short first. The OneDrive desktop client
does the upload; n8n moves each folder to posted/ once YouTube has it.

Copy comes from shorts/<slug>/posts_copy.json when it has an entry for the Short (see
plan_posts.py), else from the meta JSON's title/description/hashtags. Shorts that are flagged
("flagged" in the meta), already exported, or already posted by hand are skipped. Each exported
meta JSON gets a `queue` block recording where it went.

Usage:
    python clipper/export_posts.py shorts/<slug> [--to <OneDrive>/StreamVault] [--min-score 7]
"""

from __future__ import annotations

import _env

import argparse
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from plan_posts import TITLE_MAX, build_description, build_tags


def post_for(meta: dict, nn: str, copy: dict | None) -> dict:
    entry = (copy or {}).get("posts", {}).get(nn)
    if entry and meta.get("source_url"):
        title = entry["title"]
        description = build_description(entry, copy, meta["source_url"])
        tags = build_tags(entry, copy)
    else:
        title = meta.get("title", "")
        description = meta.get("description", "")
        tags = [h.lstrip("#") for h in meta.get("hashtags", [])]
    return {"title": title[:TITLE_MAX], "description": description, "tags": tags}


def main() -> None:
    ap = argparse.ArgumentParser(description="Copy rendered Shorts into OneDrive ready/ for n8n")
    ap.add_argument("folder", type=Path, help="shorts/<slug>")
    ap.add_argument("--to", type=Path, help="OneDrive StreamVault folder (default: STREAMVAULT_ONEDRIVE)")
    ap.add_argument("--min-score", type=float, default=0)
    args = ap.parse_args()

    root = args.to or _env.onedrive_dir()
    ready = root / "ready"
    ready.mkdir(parents=True, exist_ok=True)
    (root / "posted").mkdir(exist_ok=True)

    copy_path = args.folder / "posts_copy.json"
    copy = json.loads(copy_path.read_text(encoding="utf-8")) if copy_path.exists() else None
    slug = args.folder.resolve().name
    batch = datetime.now().strftime("%Y%m%d-%H%M%S")

    exported = skipped = 0
    for meta_path in sorted(args.folder.glob("[0-9][0-9]_*.json")):
        video = meta_path.with_suffix(".mp4")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        nn = meta_path.name[:2]
        why = ("no rendered video" if not video.exists()
               else "already exported" if meta.get("queue", {}).get("exported_at")
               else f"flagged: {meta['flagged']}" if meta.get("flagged")
               else "already posted" if meta.get("youtube", {}).get("studio_status") in ("published", "scheduled")
               else "below min score" if meta.get("score", 10) < args.min_score
               else "")
        if why:
            print(f"skip {meta_path.name}: {why}")
            skipped += 1
            continue

        post_id = f"{batch}_{nn}_{slug[:40]}"
        dest = ready / post_id
        dest.mkdir()
        shutil.copy2(video, dest / "video.mp4")
        post = {
            "version": 1,
            "id": post_id,
            **post_for(meta, nn, copy),
            "category": meta.get("category"),
            "score": meta.get("score"),
            "creator": meta.get("creator"),
            "source_url": meta.get("source_url"),
            "source_timestamps": meta.get("source_timestamps"),
            "rendered_file": video.name,
            "video": "video.mp4",
            "video_bytes": video.stat().st_size,
            "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        tmp = dest / "post.json.tmp"
        tmp.write_text(json.dumps(post, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, dest / "post.json")

        meta["queue"] = {"exported_at": post["exported_at"], "post_id": post_id}
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"READY {dest}")
        exported += 1

    print(f"exported {exported}, skipped {skipped}")


if __name__ == "__main__":
    main()
