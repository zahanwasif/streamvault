"""Engine behind the StreamVault desktop app: turns queued links and videos into Shorts for posting.

The app (app/) starts this with --watch and talks to it through files:
    .worker/requests/*.json   the app's commands, applied in name order and then deleted:
        {"op": "add", "kind": "url"|"file", "target": "...", "section": "1:00:00-2:30:00", "priority": "high"}
        {"op": "retry"|"cancel"|"remove", "id": "j0007"}
        {"op": "priority", "id": "j0007", "priority": "low"}   (the app re-prioritises posts already exported)
    .worker/state.json        every job, its stage and result (only this script writes it)
    .worker/logs/<id>.log     full output of each job's steps
stdout carries `EVENT {json}` lines (ready / job / log / removed / idle) so the app updates live.

Each job runs these stages, one job at a time, highest priority first, then oldest:
    download    clipper/fetch.py                       → sources/<slug>/
    transcribe  clipper/transcribe_long.py --engine local (VoiceStudio WhisperX on the GPU)
    pack        video-use/helpers/pack_transcripts.py  → edit/takes_packed.md
    signals     clipper/signals.py                     → edit/signals.md
    clip        headless Claude Code, shorts-clipper skill from step 3 → shorts/<slug>/
    export      clipper/export_posts.py                → outbox/<post id>/ (video.mp4 + post.json)
The app uploads each outbox folder to online OneDrive (StreamVault/ready/<post id>/) and deletes it;
the n8n poster takes it from there. The engine itself never talks to OneDrive.

A failed job is retried after 10 minutes, up to --max-attempts in total. To cancel a running job,
the app writes a cancel request and then kills this process tree; on restart the job is cancelled.
With --parent-pipe the engine exits (stopping its current step) when its stdin closes, so it never
outlives the app. Only one engine runs at a time: a second one exits with code 3, and
.worker/engine.pid names the one that holds the lock.

Usage:
    uv run --project video-use python clipper/run_queue.py --watch [--outbox DIR] [--delete-video]
    uv run --project video-use python clipper/run_queue.py            # run what's queued, then exit
"""

from __future__ import annotations

import _env

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

WORKER = _env.ROOT / ".worker"
STATE = WORKER / "state.json"
LOGS = WORKER / "logs"
REQUESTS = WORKER / "requests"
PIDFILE = WORKER / "engine.pid"
LOCKED_EXIT = 3
PRIORITIES = ("high", "normal", "low")
RETRY_AFTER = timedelta(minutes=10)
HOUR = 3600

DEFAULT_TOOLS = ",".join([
    "Read", "Write", "Edit", "Glob", "Grep", "Agent", "Task",
    "Bash(uv run:*)", "Bash(ffmpeg:*)", "Bash(ffprobe:*)", "Bash(mkdir:*)", "Bash(ls:*)",
])

PROMPT = """Headless run from the StreamVault app: nobody is watching this session and nobody can answer questions.

Use the shorts-clipper skill on the source registered at sources/{slug}. Steps 1 and 2 are done: \
the transcript, edit/takes_packed.md and edit/signals.md exist. Start at step 3 and run through \
step 10 fully automatically.{settings}

Then:
- Write shorts/{slug}/posts_copy.json as described in the skill's "Posting to YouTube" step 1. Do \
not run plan_posts.py and do not upload anything: the app and n8n post the Shorts.
- For every Short you flag in index.md, add "flagged": "<reason>" to its meta JSON so it waits \
for a human to review it.

Bash only allows uv run, ffmpeg, ffprobe, mkdir and ls, so spell commands out in full \
(`uv run --project video-use python clipper/...`) instead of using a $PY variable.

If you hit a hard failure you cannot fix, write the reason to shorts/{slug}/FAILED.txt and stop."""


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def emit(kind: str, **data) -> None:
    print("EVENT " + json.dumps({"type": kind, **data}, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------- state, lock, requests

def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"seq": 0, "jobs": {}}


def save_state(state: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE)


def update(state: dict, job: dict, **changes) -> None:
    job.update(changes)
    save_state(state)
    emit("job", job=job)


def take_lock():
    """Hold an OS lock on .worker/lock for the life of the process; None if another engine has it."""
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


def apply_requests(state: dict) -> None:
    jobs = state["jobs"]
    for req_path in sorted(REQUESTS.glob("*.json")):
        try:
            req = json.loads(req_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # still being written; next poll
        op, job = req.get("op"), jobs.get(req.get("id", ""))
        if op == "add" and req.get("target"):
            state["seq"] += 1
            jid = f"j{state['seq']:04d}"
            prio = req.get("priority") if req.get("priority") in PRIORITIES else "normal"
            jobs[jid] = {
                "id": jid, "seq": state["seq"], "kind": req.get("kind", "url"), "target": req["target"],
                "section": req.get("section") or None, "priority": prio, "status": "queued", "stage": None,
                "attempts": 0, "added": now(), "title": None, "slug": None, "error": None,
                "shorts_rendered": 0, "posts": [],
            }
            emit("job", job=jobs[jid])
        elif job and op == "retry" and job["status"] in ("failed", "cancelled", "retry"):
            job.update(status="queued", attempts=0, error=None, retry_at=None)
            emit("job", job=job)
        elif job and op == "cancel" and job["status"] in ("queued", "retry"):
            job.update(status="cancelled", stage=None, error="cancelled", finished=now())
            emit("job", job=job)
        elif job and op == "remove" and job["status"] != "running":
            del jobs[job["id"]]
            emit("removed", id=job["id"])
        elif job and op == "priority" and req.get("priority") in PRIORITIES:
            job["priority"] = req["priority"]
            emit("job", job=job)
        req_path.unlink(missing_ok=True)
    save_state(state)


def next_job(state: dict) -> dict | None:
    t = now()
    ready = [j for j in state["jobs"].values()
             if j["status"] == "queued" or (j["status"] == "retry" and (j.get("retry_at") or "") <= t)]
    ready.sort(key=lambda j: (PRIORITIES.index(j["priority"]), j["seq"]))
    return ready[0] if ready else None


# ---------------------------------------------------------------- running steps

class StepFailed(Exception):
    pass


CURRENT: list[subprocess.Popen] = []   # the step that is running now, for kill_current()


def kill_current() -> None:
    """Stop the running step and everything it started (Claude spawns its own children)."""
    for p in CURRENT:
        if p.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
            else:
                try:
                    os.killpg(p.pid, 9)
                except OSError:
                    p.kill()


def exit_with_parent() -> None:
    """--parent-pipe: the app holds our stdin open; EOF means it's gone, so stop."""
    def watch() -> None:
        while sys.stdin.buffer.read(1024):
            pass
        kill_current()
        os._exit(0)
    threading.Thread(target=watch, daemon=True).start()


def run(name: str, cmd: list[str], log: Path, timeout: float, on_line=None) -> list[str]:
    """Run one step, streaming its output to the log (and on_line), and return the output lines."""
    lines: list[str] = []
    timed_out = threading.Event()
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(f"\n=== {name}  [{now()}]\n")
        fh.flush()
        p = subprocess.Popen(cmd, cwd=_env.ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                             start_new_session=os.name != "nt")
        CURRENT[:] = [p]

        def kill() -> None:
            timed_out.set()
            kill_current()

        timer = threading.Timer(timeout, kill)
        timer.start()
        try:
            for line in p.stdout:
                fh.write(line)
                fh.flush()
                line = line.rstrip("\n")
                lines.append(line)
                if on_line:
                    on_line(line)
            p.wait()
        finally:
            timer.cancel()
            CURRENT.clear()
    if timed_out.is_set():
        raise StepFailed(f"{name} timed out after {timeout / HOUR:g} h")
    if p.returncode != 0:
        tail = [ln for ln in lines if ln.strip() and not ln.startswith("{")][-3:]
        raise StepFailed(f"{name} failed (exit {p.returncode}): {' | '.join(tail)[:400]}")
    return lines


def claude_cmd(prompt: str, tools: str) -> list[str]:
    exe = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not exe:
        raise StepFailed("Claude Code CLI not found (install it, or set CLAUDE_BIN)")
    return [exe, "-p", prompt, "--output-format", "stream-json", "--verbose", "--allowedTools", tools]


def describe_tool(block: dict) -> str:
    inp = block.get("input") or {}
    detail = inp.get("description") or inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
    detail = str(detail).splitlines()[0][:140] if detail else ""
    return f"{block.get('name')}: {detail}"


def count_shorts(shorts: Path) -> int:
    return len(list(shorts.glob("[0-9][0-9]_*.mp4"))) if shorts.exists() else 0


def process(state: dict, job: dict, args: argparse.Namespace) -> None:
    py = sys.executable
    log = LOGS / f"{job['id']}.log"

    def stage(name: str) -> None:
        update(state, job, stage=name)

    def relay(line: str) -> None:
        if line.strip():
            emit("log", id=job["id"], line=line[:300])

    stage("download")
    cmd = [py, str(_env.ROOT / "clipper" / "fetch.py"), job["target"]]
    if job.get("section"):
        cmd += ["--section", job["section"]]
    out = run("download", cmd, log, 4 * HOUR, relay)
    sources = [ln[7:].strip() for ln in out if ln.startswith("SOURCE ")]
    if not sources:
        raise StepFailed("fetch.py printed no SOURCE line")
    src = Path(sources[0])
    slug = src.name
    info = json.loads((src / "info.json").read_text(encoding="utf-8"))
    shorts = _env.SHORTS / slug
    update(state, job, slug=slug, title=info.get("title") or slug)

    if (shorts / "index.md").exists():
        emit("log", id=job["id"], line="already clipped; exporting only")
    else:
        stage("transcribe")
        run("transcribe", [py, str(_env.ROOT / "clipper" / "transcribe_long.py"), str(src), "--engine", "local"],
            log, 12 * HOUR, relay)
        stage("pack")
        run("pack", [py, str(_env.HELPERS / "pack_transcripts.py"), "--edit-dir", str(src / "edit")],
            log, 1 * HOUR, relay)
        stage("signals")
        run("signals", [py, str(_env.ROOT / "clipper" / "signals.py"), str(src)], log, 1 * HOUR, relay)

        stage("clip")
        (shorts / "FAILED.txt").unlink(missing_ok=True)
        wanted = [f"minimum score {args.min_score:g}" if args.min_score else "",
                  f"at most {args.max_shorts} Shorts" if args.max_shorts else ""]
        wanted = [w for w in wanted if w]
        settings = f" User settings for this run: {', '.join(wanted)}." if wanted else ""
        result: dict = {}

        def on_claude(line: str) -> None:
            try:
                ev = json.loads(line)
            except ValueError:
                return
            if ev.get("type") == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        emit("log", id=job["id"], line=describe_tool(block))
                n = count_shorts(shorts)
                if n != job.get("shorts_rendered"):
                    update(state, job, shorts_rendered=n)
            elif ev.get("type") == "result":
                result.update(ev)

        run("clip", claude_cmd(PROMPT.format(slug=slug, settings=settings), args.tools), log,
            args.claude_hours * HOUR, on_claude)
        job["claude"] = {"cost_usd": result.get("total_cost_usd"), "turns": result.get("num_turns"),
                         "report": (result.get("result") or "")[-1500:]}
        if result.get("is_error"):
            raise StepFailed(f"Claude run ended with an error: {(result.get('result') or '')[:300]}")
        if (shorts / "FAILED.txt").exists():
            raise StepFailed((shorts / "FAILED.txt").read_text(encoding="utf-8").strip()[:400])
        if not (shorts / "index.md").exists():
            raise StepFailed("Claude finished without writing shorts/<slug>/index.md")

    stage("export")
    out = run("export", [py, str(_env.ROOT / "clipper" / "export_posts.py"), str(shorts),
                         "--to", str(args.outbox), "--priority", job["priority"]], log, 1 * HOUR, relay)
    new = [Path(ln[6:].split("  (on hold")[0].strip()).name for ln in out if ln.startswith("READY ")]
    update(state, job, posts=job.get("posts", []) + new, shorts_rendered=count_shorts(shorts))

    if args.delete_video:
        video = Path(info.get("video", ""))
        # Only downloads this pipeline made; local files the user added are never deleted.
        if video.exists() and video.parent == src:
            video.unlink()
            emit("log", id=job["id"], line=f"deleted {video.name}")


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="StreamVault engine: queued links/videos → Shorts in the outbox")
    ap.add_argument("--watch", action="store_true", help="keep running and wait for new jobs (the app uses this)")
    ap.add_argument("--outbox", type=Path, default=_env.ROOT / "outbox",
                    help="where finished Shorts go for the app to upload (default: outbox/)")
    ap.add_argument("--max-attempts", type=int, default=2)
    ap.add_argument("--claude-hours", type=float, default=8, help="time limit for one Claude run")
    ap.add_argument("--min-score", type=float, default=0, help="override the skill's minimum score")
    ap.add_argument("--max-shorts", type=int, default=0, help="override the skill's maximum Shorts per source")
    ap.add_argument("--tools", default=os.environ.get("STREAMVAULT_CLAUDE_TOOLS", DEFAULT_TOOLS),
                    help="--allowedTools for the headless Claude run")
    ap.add_argument("--delete-video", action="store_true",
                    help="delete a downloaded source video once its Shorts are exported")
    ap.add_argument("--parent-pipe", action="store_true", help="exit when stdin closes (the app uses this)")
    args = ap.parse_args()
    if args.parent_pipe:
        exit_with_parent()

    for d in (args.outbox, LOGS, REQUESTS):
        d.mkdir(parents=True, exist_ok=True)

    lock = take_lock()
    if lock is None:
        print("another engine is running; exiting")
        return LOCKED_EXIT
    PIDFILE.write_text(str(os.getpid()), encoding="utf-8")

    state = load_state()
    for j in state["jobs"].values():
        if j["status"] == "running":  # the PC or app stopped mid-job; we hold the lock, so it isn't running
            j["status"] = "retry" if j["attempts"] < args.max_attempts else "failed"
            j["retry_at"] = None
    apply_requests(state)
    emit("ready", jobs=list(state["jobs"].values()))

    idle = False
    while True:
        apply_requests(state)
        job = next_job(state)
        if job is None:
            if not args.watch:
                return 0
            if not idle:
                emit("idle")
                idle = True
            time.sleep(3)
            continue
        idle = False

        update(state, job, status="running", attempts=job["attempts"] + 1, started=now(), error=None)
        try:
            process(state, job, args)
            update(state, job, status="done", stage=None, finished=now())
        except Exception as e:  # noqa: BLE001  - one bad job must not stop the queue
            failed = job["attempts"] >= args.max_attempts
            retry_at = None if failed else (datetime.now().astimezone() + RETRY_AFTER).isoformat(timespec="seconds")
            update(state, job, status="failed" if failed else "retry", error=str(e), finished=now(),
                   retry_at=retry_at)


if __name__ == "__main__":
    sys.exit(main())
