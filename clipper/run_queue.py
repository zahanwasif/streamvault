"""Queue worker for the PC: process links and videos dropped in OneDrive, one at a time.

    <OneDrive>/StreamVault/queue/*.txt|.url|.md    one link per line, optionally `--section 1:00:00-2:30:00`
    <OneDrive>/StreamVault/queue/*.mp4|.mkv|...    video files (moved to inbox/queue/ on this PC)
    <OneDrive>/StreamVault/queue/done/             text files whose links have all been handled
    <OneDrive>/StreamVault/status.md               what the worker has done, readable from the phone

For each item, in the order it arrived:
  1. clipper/fetch.py downloads or registers it                → sources/<slug>/
  2. headless Claude Code runs the shorts-clipper skill        → shorts/<slug>/ + posts_copy.json
  3. clipper/export_posts.py copies the Shorts to OneDrive      → ready/<post id>/
OneDrive syncs ready/ to the cloud, and the n8n poster takes it from there.

A failed item is retried on the next run, up to --max-attempts in total. State lives in
.worker/state.json and logs in .worker/logs/. Only one worker runs at a time (lock file), so
Task Scheduler can start this every 30 minutes and a long VOD just makes later starts exit.

Usage:
    uv run --project video-use python clipper/run_queue.py [--delete-video] [--dry-run]
"""

from __future__ import annotations

import _env

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

WORKER = _env.ROOT / ".worker"
STATE = WORKER / "state.json"
LOGS = WORKER / "logs"
LOCAL_QUEUE = _env.INBOX / "queue"
TEXT_EXTS = {".txt", ".url", ".md"}
URL_RE = re.compile(r"https?://[^\s\"'<>]+")
SECTION_RE = re.compile(r"(?:--section|section\s*[=:])\s*(\d[\d:]*-\d[\d:]*)", re.I)

DEFAULT_TOOLS = ",".join([
    "Read", "Write", "Edit", "Glob", "Grep", "Agent", "Task",
    "Bash(uv run:*)", "Bash(ffmpeg:*)", "Bash(ffprobe:*)", "Bash(mkdir:*)", "Bash(ls:*)",
])

PROMPT = """Headless queue run: nobody is watching this session and nobody can answer questions.

Use the shorts-clipper skill on the source already registered at sources/{slug} (its info.json has \
the video path and URL). Skip step 1, it is done. Run steps 2-10 fully automatically.

Then:
- Write shorts/{slug}/posts_copy.json as described in the skill's "Posting to YouTube" step 1. Do \
not run plan_posts.py and do not upload anything: the queue posts the Shorts.
- For every Short you flag in index.md, add "flagged": "<reason>" to its meta JSON so the queue \
holds it back for a human to look at.

Bash only allows uv run, ffmpeg, ffprobe, mkdir and ls, so spell commands out in full \
(`uv run --project video-use python clipper/...`) instead of using a $PY variable.

If you hit a hard failure you cannot fix, write the reason to shorts/{slug}/FAILED.txt and stop."""


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- state + lock

def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"seq": 0, "items": {}}


def save_state(state: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE)


def take_lock():
    """Hold an OS lock on .worker/lock for the life of the process; None if another worker has it."""
    fh = open(WORKER / "lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


# ---------------------------------------------------------------- queue scan

def is_settled(p: Path, quiet_s: int = 120) -> bool:
    """A dropped video is still syncing while its size or mtime keeps changing."""
    a = p.stat()
    if time.time() - a.st_mtime < quiet_s:
        return False
    time.sleep(2)
    return p.stat().st_size == a.st_size


def links_in(p: Path) -> list[tuple[str, str, str | None]]:
    """(state key, url, section) for every link in a queue text file."""
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = URL_RE.search(line)
        if m:
            url = m.group(0).rstrip(").,;]")
            sec = SECTION_RE.search(line)
            section = sec.group(1) if sec else None
            out.append((f"url:{url}|{section or ''}", url, section))
    return out


def scan(queue: Path, state: dict, dry: bool) -> None:
    """Register anything new in the OneDrive queue folder. A link that was queued before is not
    queued again."""
    items = state["items"]

    def add(key: str, **item) -> None:
        state["seq"] += 1
        items[key] = {"seq": state["seq"], "status": "queued", "attempts": 0, "added": now(), **item}
        print(f"queued #{state['seq']}: {item['target']}")

    files = sorted((p for p in queue.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime)
    for p in files:
        ext = p.suffix.lower()
        if ext in TEXT_EXTS:
            for key, url, section in links_in(p):
                if key not in items:
                    add(key, kind="url", target=url, section=section)
        elif ext in _env.VIDEO_EXTS and is_settled(p):
            key = f"file:{p.name}:{p.stat().st_size}"
            if key in items:
                continue
            dest = LOCAL_QUEUE / p.name
            if not dry:
                LOCAL_QUEUE.mkdir(parents=True, exist_ok=True)
                n = 1
                while dest.exists():
                    n += 1
                    dest = LOCAL_QUEUE / f"{p.stem} ({n}){p.suffix}"
                shutil.move(str(p), dest)  # also frees the OneDrive space
            add(key, kind="file", target=str(dest), section=None)


def retire_lists(queue: Path, state: dict) -> None:
    """Move link files to queue/done/ once every link in them is done or has failed for good."""
    items = state["items"]
    for p in queue.iterdir():
        if not (p.is_file() and p.suffix.lower() in TEXT_EXTS):
            continue
        keys = [k for k, _, _ in links_in(p)]
        if keys and all(items.get(k, {}).get("status") in ("done", "failed") for k in keys):
            (queue / "done").mkdir(exist_ok=True)
            shutil.move(str(p), queue / "done" / f"{datetime.now():%Y%m%d-%H%M%S}_{p.name}")


# ---------------------------------------------------------------- processing

class StepFailed(Exception):
    pass


def run(name: str, cmd: list[str], log: Path, timeout: float) -> str:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(f"\n=== {name}  [{now()}]\n")
        fh.flush()
        try:
            p = subprocess.run(cmd, cwd=_env.ROOT, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            fh.write("TIMEOUT\n")
            raise StepFailed(f"{name} timed out") from None
        fh.write(p.stdout)
        fh.write(p.stderr)
    if p.returncode != 0:
        tail = (p.stderr.strip() or p.stdout.strip()).splitlines()[-3:]
        raise StepFailed(f"{name} exited {p.returncode}: {' | '.join(tail)[:400]}")
    return p.stdout


def claude_cmd(prompt: str, tools: str) -> list[str]:
    exe = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not exe:
        raise StepFailed("Claude Code CLI not found (install it, or set CLAUDE_BIN)")
    return [exe, "-p", prompt, "--output-format", "json", "--allowedTools", tools]


def process(item: dict, args: argparse.Namespace, onedrive: Path) -> None:
    py = sys.executable
    log = LOGS / f"{item['seq']:04d}.log"
    hours = 3600

    cmd = [py, str(_env.ROOT / "clipper" / "fetch.py"), item["target"]]
    if item.get("section"):
        cmd += ["--section", item["section"]]
    out = run("fetch", cmd, log, 4 * hours)
    sources = [line[7:].strip() for line in out.splitlines() if line.startswith("SOURCE ")]
    if not sources:
        raise StepFailed("fetch.py printed no SOURCE line")
    src = Path(sources[0])
    slug = src.name
    item["slug"] = slug
    shorts = _env.SHORTS / slug

    (shorts / "FAILED.txt").unlink(missing_ok=True)
    if (shorts / "index.md").exists():
        print(f"  {slug}: already clipped, exporting only")
    else:
        print(f"  {slug}: clipping with Claude Code (this can take a while)")
        out = run("claude", claude_cmd(PROMPT.format(slug=slug), args.tools), log, args.claude_hours * hours)
        try:
            res = json.loads(out)
            item["claude"] = {"cost_usd": res.get("total_cost_usd"), "turns": res.get("num_turns"),
                              "report": (res.get("result") or "")[-1500:]}
            if res.get("is_error"):
                raise StepFailed(f"Claude run ended with an error: {(res.get('result') or '')[:300]}")
        except ValueError:
            pass
        if (shorts / "FAILED.txt").exists():
            raise StepFailed((shorts / "FAILED.txt").read_text(encoding="utf-8").strip()[:400])
        if not (shorts / "index.md").exists():
            raise StepFailed("Claude finished without writing shorts/<slug>/index.md")

    out = run("export", [py, str(_env.ROOT / "clipper" / "export_posts.py"), str(shorts), "--to", str(onedrive)],
              log, 1 * hours)
    item["exported"] = item.get("exported", 0) + sum(1 for line in out.splitlines() if line.startswith("READY "))

    if args.delete_video:
        info = json.loads((src / "info.json").read_text(encoding="utf-8"))
        video = Path(info.get("video", ""))
        # Only files this pipeline put here: downloads in sources/ and videos taken from the queue.
        if video.exists() and (video.parent == src or video.parent == LOCAL_QUEUE):
            video.unlink()
            print(f"  deleted {video.name}")


# ---------------------------------------------------------------- status

def write_status(onedrive: Path, state: dict) -> None:
    rows = sorted(state["items"].values(), key=lambda i: -i["seq"])[:40]
    counts: dict[str, int] = {}
    for i in state["items"].values():
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    ready = sum(1 for p in (onedrive / "ready").iterdir() if p.is_dir()) if (onedrive / "ready").is_dir() else 0
    lines = ["# StreamVault queue", "",
             f"Updated {datetime.now():%a %b %d %H:%M} (PC time). "
             + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + f". Shorts waiting to post: {ready}.", "",
             "| # | Added | Source | Status | Shorts | Note |", "|---|---|---|---|---|---|"]
    for i in rows:
        src = i["target"] if i["kind"] == "url" else Path(i["target"]).name
        note = (i.get("error") or "").replace("|", "/").replace("\n", " ")[:120]
        lines.append(f"| {i['seq']} | {i['added'][:16].replace('T', ' ')} | {src} | {i['status']} "
                     f"| {i.get('exported', '')} | {note} |")
    (onedrive / "status.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Process the OneDrive link/video queue, one item at a time")
    ap.add_argument("--onedrive", type=Path, help="OneDrive StreamVault folder (default: STREAMVAULT_ONEDRIVE)")
    ap.add_argument("--max-attempts", type=int, default=2)
    ap.add_argument("--claude-hours", type=float, default=8, help="time limit for one Claude run")
    ap.add_argument("--tools", default=os.environ.get("STREAMVAULT_CLAUDE_TOOLS", DEFAULT_TOOLS),
                    help="--allowedTools for the headless Claude run")
    ap.add_argument("--delete-video", action="store_true",
                    help="delete the downloaded/queued source video after its Shorts are exported")
    ap.add_argument("--dry-run", action="store_true", help="list the queue and exit")
    args = ap.parse_args()

    onedrive = args.onedrive or _env.onedrive_dir()
    queue = onedrive / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    (onedrive / "ready").mkdir(exist_ok=True)
    (onedrive / "posted").mkdir(exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)

    lock = take_lock()
    if lock is None:
        print("another queue worker is running; exiting")
        return 0

    state = load_state()
    for i in state["items"].values():
        if i["status"] == "running":  # the PC shut down mid-run; we hold the lock, so it's not running now
            i["status"] = "retry" if i["attempts"] < args.max_attempts else "failed"
    while True:
        scan(queue, state, args.dry_run)
        pending = sorted((i for i in state["items"].values() if i["status"] in ("queued", "retry")),
                         key=lambda i: i["seq"])
        if args.dry_run:
            for i in pending:
                print(f"#{i['seq']} {i['status']}: {i['target']}" + (f" [{i['section']}]" if i.get("section") else ""))
            print(f"{len(pending)} pending")
            return 0
        if not pending:
            break

        item = pending[0]
        item["status"] = "running"
        item["attempts"] += 1
        item["started"] = now()
        save_state(state)
        write_status(onedrive, state)
        print(f"#{item['seq']} ({item['attempts']}/{args.max_attempts}): {item['target']}")
        try:
            process(item, args, onedrive)
            item["status"] = "done"
            item.pop("error", None)
            print(f"  done: {item.get('exported', 0)} Short(s) sent to OneDrive")
        except Exception as e:  # noqa: BLE001  - one bad item must not stop the queue
            item["error"] = str(e)
            item["status"] = "failed" if item["attempts"] >= args.max_attempts else "retry"
            print(f"  {item['status']}: {e}")
        item["finished"] = now()
        save_state(state)
        retire_lists(queue, state)
        save_state(state)
        write_status(onedrive, state)
        if item["status"] == "retry":
            break  # try it again on the next scheduled run, not straight away

    retire_lists(queue, state)
    save_state(state)
    write_status(onedrive, state)
    print("queue empty" if not any(i["status"] in ("queued", "retry") for i in state["items"].values())
          else "stopping; retrying failed item next run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
